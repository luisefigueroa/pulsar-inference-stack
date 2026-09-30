"""Compile the effective spec and compare actual container configuration.

This module has no topology discovery, transport, storage mutation or Docker
execution. Shell lifecycle code supplies verified placement and prepared files.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
import os
from pathlib import PurePosixPath
import re
import secrets

from release_spec import serving
from release_spec.memory_estimate import validate_frozen as validate_memory_estimate
from release_spec.normalize import canonical_json_digest

PLAN_SCHEMA_VERSION = 6
SELECTED_SPEC_LABEL = "io.pulsar.gb10.selected-spec-id"
SPEC_LABEL = "io.pulsar.gb10.spec-id"
PLAN_LABEL = "io.pulsar.gb10.launch-plan"
PREFIX = "io.pulsar.gb10."


def fail(message):
    raise ValueError(message)


def text(value, field):
    if not isinstance(value, str) or not value or any(c in value for c in "\0\n\r\t"):
        fail(f"{field}: expected nonempty single-line text")
    return value


def digest(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        fail(f"{field}: expected a content digest")
    return value


def service_identifier(selected_spec_id, topology_id, node_ids):
    return canonical_json_digest({'selected_spec_id':selected_spec_id,
                                  'topology_id':topology_id,'node_ids':node_ids})


def prepared_snapshots(spec, prepared, topology_id):
    """Check the complete named snapshot/rank matrix before binding local paths."""
    expected = serving.required_snapshots(spec)
    if spec['schema_version'] == 2:
        members = {'target': prepared}
    else:
        serving.closed(prepared, {'schema_version','kind','spec_id','topology_id','snapshots'}, 'prepared set')
        if type(prepared['schema_version']) is not int or prepared['schema_version'] != 2 or prepared['kind'] != 'pulsar-prepared-set':
            fail('unsupported prepared-set schema')
        if prepared['spec_id'] != spec['spec_id'] or prepared['topology_id'] != topology_id:
            fail('prepared set identity differs')
        members = serving.closed(prepared['snapshots'], set(expected), 'prepared snapshots')
    nodes = None
    for name, model in expected.items():
        member = members[name]
        if not isinstance(member, dict) or type(member.get('schema_version')) is not int or member['schema_version'] != 1 or member.get('kind') != 'pulsar-prepared-set':
            fail('invalid prepared snapshot: ' + name)
        for key, value in [('spec_id',spec['spec_id']),('topology_id',topology_id),
                           ('snapshot_manifest_id',model['snapshot_manifest']['manifest_id']),('revision',model['model_commit'])]:
            if member.get(key) != value: fail('prepared snapshot ' + name + ' differs: ' + key)
        ranks = member.get('ranks')
        if not isinstance(ranks,list) or len(ranks) != spec['recipe']['geometry']['nodes']:
            fail('prepared snapshot must cover every rank: ' + name)
        ids = []
        for index, row in enumerate(ranks):
            if type(row.get('rank')) is not int or row['rank'] != index: fail('invalid prepared rank order')
            ids.append(text(row.get('node_id'), 'node_id'))
            hub = PurePosixPath(text(row.get('hub_path'), 'hub_path'))
            if not hub.is_absolute() or '..' in hub.parts or row.get('path') != str(hub/'snapshots'/model['model_commit']):
                fail('prepared rank paths do not select the verified snapshot')
            if spec['schema_version'] == 3 and row.get('snapshot_manifest_id') != model['snapshot_manifest']['manifest_id']:
                fail('prepared rank manifest differs: ' + name)
        if len(set(ids)) != len(ids) or member.get('home_node_id') not in ids:
            fail('snapshot home must belong to the exact serving nodes')
        if nodes is not None and nodes != ids: fail('required snapshots have different rank placement')
        nodes = ids
    return members


def build_plan(spec, selected_spec_id, facts, prepared, *, selected_spec=None):
    spec = serving.verify_spec(spec)
    selected_spec_id = digest(selected_spec_id, "selected_spec_id")
    selected_spec = serving.verify_spec(selected_spec if selected_spec is not None else spec)
    if selected_spec['spec_id'] != selected_spec_id:
        fail('selected spec document differs from selected identity')
    # Overrides cannot change model bytes, image or placement geometry.
    for key in ('model', 'image_digest', 'geometry', *(['required_snapshots'] if spec['schema_version']==3 else [])):
        if spec['recipe'][key] != selected_spec['recipe'][key]:
            fail(f'effective recipe cannot override {key}')
    recipe = spec["recipe"]
    model = recipe["model"]
    count = recipe["geometry"]["nodes"]
    topology = digest(facts.get("topology_id"), "topology_id")
    members = prepared_snapshots(selected_spec, prepared, topology)
    prepared = members["target"]
    serving.snapshot_engine_args(recipe)
    if (not isinstance(prepared, dict) or prepared.get("kind") != "pulsar-prepared-set"
            or type(prepared.get("schema_version")) is not int or prepared["schema_version"] != 1):
        fail("invalid prepared-set document")
    for key, expected in (("spec_id", selected_spec_id), ("topology_id", topology),
                          ("snapshot_manifest_id", model["snapshot_manifest"]["manifest_id"]),
                          ("revision", model["model_commit"])):
        if prepared.get(key) != expected:
            fail(f"prepared {key} differs from selected spec or topology")
    if not isinstance(facts.get("ranks"), list) or not isinstance(prepared.get("ranks"), list):
        fail("missing ordered ranks")
    if len(facts["ranks"]) != count or len(prepared["ranks"]) != count:
        fail("prepared set and placement must cover every required rank")
    ranks = []
    for index, (placement, files) in enumerate(zip(facts["ranks"], prepared["ranks"])):
        if (type(placement.get("rank")) is not int or placement["rank"] != index
                or type(files.get("rank")) is not int or files["rank"] != index
                or files.get("node_id") != placement.get("node_id")):
            fail("prepared rank placement differs from confirmed topology")
        rank = {key: text(placement.get(key), f"rank {index} {key}")
                for key in ("node_id", "hostname", "ssh_host", "control_ip", "control_if")}
        rank.update(rank=index, hcas=placement.get("hcas", ""), hub_path=text(files.get("hub_path"), "hub_path"))
        if count > 1:
            text(rank["hcas"], "fabric devices")
        hub = PurePosixPath(rank["hub_path"])
        if not hub.is_absolute() or ".." in hub.parts or files.get("path") != str(hub / "snapshots" / model["model_commit"]):
            fail("prepared rank paths do not select the verified snapshot")
        if spec["schema_version"] == 3:
            rank["snapshots"] = {name: {"hub_path": member["ranks"][index]["hub_path"],
                "home_node_id": member["home_node_id"]} for name, member in members.items()}
        ranks.append(rank)
    if len({rank["node_id"] for rank in ranks}) != count:
        fail("placement repeats a physical node")
    port = serving.integer(facts.get("port"), "port", 1)
    master_port = serving.integer(facts.get("master_port", 29500), "master_port", 1)
    if port > 65535 or master_port > 65535:
        fail("ports must be in 1..65535")
    plan = {"schema_version": 4 if spec["schema_version"] == 3 else 3, "kind": "pulsar-launch-plan",
            "selected_spec_id": selected_spec_id, "spec_id": spec["spec_id"], "spec": spec,
            "selected_spec": selected_spec,
            "matches_selected_spec": selected_spec_id == spec["spec_id"],
            "container_name": ("vllm-" if count == 1 else "vllm-cluster-") + selected_spec_id,
            "port": port, "master_port": master_port,
            "served_name": text(facts.get("served_name"), "served_name"),
            "topology_id": topology, "home_node_id": text(prepared.get("home_node_id"), "home_node_id"),
            "ranks": ranks, "api_auth": bool(os.environ.get("VLLM_API_KEY") or os.environ.get("API_KEY")),
            "lifecycle_action": facts.get("lifecycle_action", "dry-run")}
    if plan["lifecycle_action"] not in ("dry-run", "start", "replace"):
        fail("unknown lifecycle action")
    if "memory_estimate" in facts:
        plan["memory_estimate"] = validate_memory_estimate(facts["memory_estimate"], spec)
        plan["schema_version"] = 5
    guard = spec["recipe"]["container"].get("guard")
    if guard:
        from serving_guard.program import digest as guard_digest, program
        source = facts.get("guard_program")
        if source is None:
            source = program()
        if not isinstance(source, str) or len(source) > 120000 or guard_digest(source) != guard["program_sha256"]:
            fail("guard program differs from the reviewed recipe")
        run_id = facts.get("guard_run_id", secrets.token_hex(32))
        if not isinstance(run_id, str) or not re.fullmatch("[0-9a-f]{64}", run_id):
            fail("invalid guard invocation identity")
        plan.update(schema_version=6, guard_program=source, guard_run_id=run_id)
    plan["service_id"] = service_identifier(selected_spec_id,topology,[rank['node_id'] for rank in ranks])
    plan["plan_id"] = canonical_json_digest(plan)
    return plan


def validate_plan(plan):
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int or plan["schema_version"] not in (3, 4, 5, 6):
        fail("unsupported launch-plan schema")
    spec = serving.verify_spec(plan["spec"])
    expected_version = (6 if spec["recipe"]["container"].get("guard") else
                        5 if "memory_estimate" in plan else spec["schema_version"] + 1)
    if plan["schema_version"] != expected_version:
        fail("launch-plan schema differs from spec schema")
    if plan.get("spec_id") != spec["spec_id"]:
        fail("launch plan spec identity differs")
    body = {key: value for key, value in plan.items() if key != "plan_id"}
    if plan.get("plan_id") != canonical_json_digest(body):
        fail("launch plan content digest differs")
    # Rebuild the structural binding without accepting unverified argv fields.
    model = spec["recipe"]["model"]
    prepared = {"schema_version": 1, "kind": "pulsar-prepared-set",
        "spec_id": plan["selected_spec_id"], "topology_id": plan["topology_id"],
        "snapshot_manifest_id": model["snapshot_manifest"]["manifest_id"],
        "revision": model["model_commit"], "home_node_id": plan["home_node_id"],
        "ranks": [{**rank, "path": str(PurePosixPath(rank["hub_path"]) / "snapshots" / model["model_commit"])}
                  for rank in plan["ranks"]]}
    if spec['schema_version'] == 3:
        snapshots = {}
        for name, item in serving.required_snapshots(spec).items():
            snapshots[name] = {'schema_version':1,'kind':'pulsar-prepared-set',
                'spec_id':plan['selected_spec_id'],'topology_id':plan['topology_id'],
                'snapshot_manifest_id':item['snapshot_manifest']['manifest_id'],'revision':item['model_commit'],
                'home_node_id':plan['ranks'][0]['snapshots'][name]['home_node_id'],
                'ranks':[{'rank':row['rank'],'node_id':row['node_id'],
                          'snapshot_manifest_id':item['snapshot_manifest']['manifest_id'],
                          'hub_path':row['snapshots'][name]['hub_path'],
                          'path':str(PurePosixPath(row['snapshots'][name]['hub_path'])/'snapshots'/item['model_commit'])}
                         for row in plan['ranks']]}
        prepared = {'schema_version':2,'kind':'pulsar-prepared-set','spec_id':plan['selected_spec_id'],
                    'topology_id':plan['topology_id'],'snapshots':snapshots}
    rebuilt = build_plan(spec, plan["selected_spec_id"], plan, prepared, selected_spec=plan['selected_spec'])
    # Authentication is a recorded site choice; validation never depends on the
    # current shell's secret. The value is applied only at actual launch.
    if type(plan.get("api_auth")) is not bool:
        fail("api_auth must be boolean")
    rebuilt["api_auth"] = plan["api_auth"]
    rebuilt.pop("plan_id")
    if rebuilt != body:
        fail("launch plan differs from its validated inputs")
    return plan


def rank_spec(plan, rank):
    validate_plan(plan)
    recipe = plan["spec"]["recipe"]
    if type(rank) is not int or not 0 <= rank < recipe["geometry"]["nodes"]:
        fail("rank is outside the serving geometry")
    row = plan["ranks"][rank]
    model = recipe["model"]
    manifest = model["snapshot_manifest"]
    target = "/root/.cache/huggingface/hub/models--" + model["model_id"].replace("/", "--")
    labels = {PREFIX + "managed": "true", PREFIX + "conf": plan["selected_spec_id"],
              PREFIX + "rank": "single" if len(plan["ranks"]) == 1 else str(rank),
              PREFIX + "world-size": str(len(plan["ranks"])), PREFIX + "topology": plan["topology_id"],
              PREFIX + "node-id": row["node_id"], PREFIX + "weight-source": "local-files",
              PREFIX + "weight-owner": plan["home_node_id"], PREFIX + "weight-config": manifest["manifest_id"][:12],
              PREFIX + "model-revision": model["model_commit"], PREFIX + "model-identity-status": "manifest-verified",
              SELECTED_SPEC_LABEL: plan["selected_spec_id"], SPEC_LABEL: plan["spec_id"], PLAN_LABEL: plan["plan_id"]}
    if recipe["container"].get("guard"):
        labels[PREFIX + "guard-run"] = plan["guard_run_id"]
    mounts = [{"source": row["hub_path"], "target": target, "mode": "ro"}]
    paths = {'target':target + '/snapshots/' + model['model_commit']}
    for name, item in recipe.get('required_snapshots', {}).items():
        destination = '/pulsar/snapshots/' + item['snapshot_manifest']['manifest_id']
        mount = {'source':row['snapshots'][name]['hub_path'],'target':destination,'mode':'ro'}
        if mount not in mounts: mounts.append(mount)
        paths[name] = destination + '/snapshots/' + item['model_commit']
    return {'labels':labels,'mounts':mounts,'model_path':paths['target'],
            'engine_args':serving.snapshot_engine_args(recipe,paths)}


def environment(plan, rank):
    recipe = plan["spec"]["recipe"]
    env = dict(item.split("=", 1) for item in recipe["container_env"])
    if len(plan["ranks"]) > 1:
        row = plan["ranks"][rank]
        env.update(VLLM_HOST_IP=row["control_ip"], NCCL_IB_HCA=row["hcas"],
                   NCCL_SOCKET_IFNAME=row["control_if"], GLOO_SOCKET_IFNAME=row["control_if"],
                   TP_SOCKET_IFNAME=row["control_if"])
    return env


def docker_argv(plan, rank, *, detach=False, include_secrets=True):
    spec = rank_spec(plan, rank)
    recipe = plan["spec"]["recipe"]
    c = recipe["container"]
    guard = c.get("guard")
    args = ["docker", "run", "--name", plan["container_name"]]
    if guard:
        args += ["--rm", "-i", "--sig-proxy=false", "--cgroupns", "private",
                 "--pids-limit", "512", "--pull", "never", "--entrypoint", "python3",
                 "--mount", "type=bind,src=/proc/meminfo,dst=/pulsar-guard-host-meminfo,readonly"]
    elif detach or len(plan["ranks"]) > 1:
        args.append("-d")
    for key, value in spec["labels"].items():
        args += ["--label", f"{key}={value}"]
    args += ["--gpus", "all", "--network", c["network_mode"], "--ipc", c["ipc_mode"]]
    if c["network_mode"] == "bridge":
        args += ["-p", f"{plan['port']}:{plan['port']}"]
    if c["shm_size_bytes"] is not None:
        args += ["--shm-size", str(c["shm_size_bytes"])]
    for name, limit in sorted(c["ulimits"].items()):
        args += ["--ulimit", f"{name}={limit['soft']}:{limit['hard']}"]
    if c["memory_limit_bytes"]:
        args += ["--memory", str(c["memory_limit_bytes"]),
                 "--memory-swap", str(c['memory_limit_bytes'] * (1 if guard else 2))]
    if c["cpu_limit_nanos"]:
        from decimal import Decimal
        args += ["--cpus", format(Decimal(c["cpu_limit_nanos"]) / 1000000000, "f")]
    for device in c["devices"]:
        args += ["--device", "/dev/" + device]
    for mount in spec["mounts"]:
        args += ["-v", f"{mount['source']}:{mount['target']}:ro"]
    for name, value in sorted(environment(plan, rank).items()):
        args += ["-e", f"{name}={value}"]
    if len(plan['ranks']) == 1:
        args += ['-e', 'HF_TOKEN=' + (os.environ.get('HF_TOKEN', '') if include_secrets else '<credential>')]
    restart = c["restart_policy"]
    if restart == "on-failure" and c["restart_max_retries"]:
        restart += ":" + str(c["restart_max_retries"])
    args += ["--restart", restart]
    health = c["healthcheck"]
    if health:
        args += ["--health-cmd", f"curl -fs http://localhost:{plan['port']}{health['path']} || exit 1",
                 "--health-interval", str(health["interval_seconds"]) + "s",
                 "--health-timeout", str(health["timeout_seconds"]) + "s",
                 "--health-retries", str(health["retries"]),
                 "--health-start-period", str(health["start_period_seconds"]) + "s"]
    else:
        args.append("--no-healthcheck")
    args += [plan["spec"]["source"]["image_repository"] + "@" + recipe["image_digest"],
             "--model", spec["model_path"], "--served-model-name", plan["served_name"],
             "--host", "0.0.0.0", "--port", str(plan["port"]), *spec["engine_args"],
             "--tensor-parallel-size", str(recipe["geometry"]["tp"]),
             "--pipeline-parallel-size", str(recipe["geometry"]["pp"])]
    if len(plan["ranks"]) > 1:
        args += ["--nnodes", str(len(plan["ranks"])), "--master-addr", plan["ranks"][0]["control_ip"],
                 "--master-port", str(plan["master_port"]), "--node-rank", str(rank)]
        if rank:
            args.append("--headless")
    if rank == 0 and plan["api_auth"]:
        key = (os.environ.get("VLLM_API_KEY") or os.environ.get("API_KEY")) if include_secrets else "<credential>"
        if not key:
            fail("API authentication was requested but its configured credential is unavailable")
        args += ["--api-key", key]
    if guard:
        image_ref = plan["spec"]["source"]["image_repository"] + "@" + recipe["image_digest"]
        offset = args.index(image_ref) + 1
        context = {"rank": rank, "run_id": plan["guard_run_id"], "spec_id": plan["spec_id"],
                   "limits": {"memory_bytes": c["memory_limit_bytes"],
                              **{k: guard[k] for k in ("min_host_available_bytes", "startup_timeout_seconds", "timeout_seconds")}}}
        if guard["schema_version"] == 2:
            context["limits"]["max_host_swap_growth_bytes"] = guard["max_host_swap_growth_bytes"]
        args[offset:] = ["-I", "-S", "-u", "-c", plan["guard_program"], json.dumps(context, sort_keys=True),
                         *guard["entrypoint"], *args[offset:]]
    return args


def container_configuration(container, image):
    config, host = container.get("Config") or {}, container.get("HostConfig") or {}
    cpu_limit=host.get('NanoCpus',0)
    # Docker documents --cpus as the default 100000-us quota/period pair.
    if host.get('CpuQuota',0)>0:
        period=host.get('CpuPeriod') or 100000
        quota_limit=host['CpuQuota']*1000000000//period
        if cpu_limit and cpu_limit != quota_limit:
            fail('container has conflicting CPU limits')
        cpu_limit=quota_limit
    return {"network_mode": host.get("NetworkMode"), "ipc_mode": host.get("IpcMode"),
            "shm_size_bytes": host.get("ShmSize") if host.get("IpcMode") == "private" else None,
            "ulimits": {row["Name"]: {"soft": row["Soft"], "hard": row["Hard"]} for row in host.get("Ulimits") or []},
            "memory_limit_bytes": host.get("Memory", 0), "cpu_limit_nanos": cpu_limit,
            'memory_swap_limit_bytes':host.get('MemorySwap',0),
            "restart_policy": (host.get("RestartPolicy") or {}).get("Name") or "no",
            "restart_max_retries": (host.get("RestartPolicy") or {}).get("MaximumRetryCount", 0),
            "devices": host.get("Devices") or [], "accelerator_requests": host.get("DeviceRequests") or [],
            "port_bindings": host.get("PortBindings") or {}, "healthcheck": config.get("Healthcheck") or {},
            "entrypoint": config.get("Entrypoint"), "command": config.get("Cmd") or [],
            "image_digest": image.get("RepoDigests") or []}


def observe_rank(plan, rank, container, image):
    expected = rank_spec(plan, rank)
    recipe = plan["spec"]["recipe"]
    config = container.get("Config") or {}
    labels = config.get("Labels") or {}
    for key, value in expected["labels"].items():
        if labels.get(key) != value:
            fail(f"rank {rank}: ownership or identity label differs ({key})")
    state = container.get("State") or {}
    if state.get("Running") is not True:
        fail(f"rank {rank}: container is not running")
    if not image.get("Id") or image["Id"] != container.get("Image"):
        fail(f"rank {rank}: actual image differs")
    if not any(ref.endswith("@" + recipe["image_digest"]) for ref in image.get("RepoDigests") or []):
        fail(f"rank {rank}: actual image digest differs from recipe")
    guard = recipe["container"].get("guard")
    image_entrypoint = (image.get("Config") or {}).get("Entrypoint")
    expected_entrypoint = ["python3"] if guard else image_entrypoint
    if guard and image_entrypoint != guard["entrypoint"]:
        fail(f"rank {rank}: pinned image entrypoint differs from guard policy")
    if config.get("Entrypoint") != expected_entrypoint:
        fail(f"rank {rank}: entrypoint differs from pinned image")
    args = docker_argv(plan, rank, include_secrets=False)
    image_ref = plan["spec"]["source"]["image_repository"] + "@" + recipe["image_digest"]
    command = list(config.get("Cmd") or [])
    if rank == 0 and plan["api_auth"]:
        if command.count("--api-key") != 1 or command.index("--api-key") + 1 >= len(command):
            fail(f"rank {rank}: API authentication binding differs")
        text(command[command.index('--api-key') + 1], 'API credential')
        command[command.index("--api-key") + 1] = "<credential>"
    if command != args[args.index(image_ref) + 1:]:
        fail(f"rank {rank}: engine command differs")
    env = dict(item.split("=", 1) for item in (image.get("Config") or {}).get("Env") or [])
    env.update(environment(plan, rank))
    actual_items = config.get("Env") or []
    actual = dict(item.split("=", 1) for item in actual_items)
    if len(plan['ranks']) == 1:
        env['HF_TOKEN'] = '<credential>'
        if 'HF_TOKEN' in actual:
            actual['HF_TOKEN'] = '<credential>'
    if len(actual) != len(actual_items) or actual != env:
        fail(f"rank {rank}: environment differs")
    mounts = container.get("Mounts") or []
    if guard:
        if any(m.get('Destination') in ('/', '/proc', '/sys', '/sys/fs', '/sys/fs/cgroup')
               or str(m.get('Destination', '')).startswith('/sys/fs/cgroup/') for m in mounts):
            fail(f'rank {rank}: mount shadows guard resource counters')
        guard_mounts = [m for m in mounts if m.get('Destination') == '/pulsar-guard-host-meminfo']
        if (len(guard_mounts) != 1 or guard_mounts[0].get('Type') != 'bind'
                or guard_mounts[0].get('Source') != '/proc/meminfo'
                or guard_mounts[0].get('RW') is not False):
            fail(f'rank {rank}: host memory guard mount differs')
        mounts = [m for m in mounts if m not in guard_mounts]
    matching = []
    targets = {mount['target'] for mount in expected['mounts']}
    for mount in expected['mounts']:
        found = [m for m in mounts if m.get('Destination') == mount['target']]
        if len(found) != 1 or found[0].get('Source') != mount['source'] or found[0].get('RW') is not False:
            fail(f'rank {rank}: model mount differs')
        if any(m.get('Destination','').startswith(mount['target'] + '/') for m in mounts):
            fail(f'rank {rank}: nested mount shadows verified files')
        matching.extend(found)
    image_volumes=(image.get('Config') or {}).get('Volumes') or {}
    for item in mounts:
        if item.get('Destination') not in targets and (
                item.get('Destination') not in image_volumes or item.get('Type')!='volume'):
            fail(f'rank {rank}: mount is not part of the recipe or pinned image')
        if item.get('Destination') not in targets and any(target.startswith(item.get('Destination','').rstrip('/') + '/') for target in targets):
            fail(f'rank {rank}: ancestor mount shadows verified files')
    observed = container_configuration(container, image)
    c = recipe["container"]
    host=container.get('HostConfig') or {}
    if host.get('Privileged') or host.get('CapAdd') or host.get('CapDrop') or host.get('ReadonlyRootfs'):
        fail(f'rank {rank}: unsupported container privilege or filesystem override')
    if (host.get('CpuPeriod',0) not in (0,100000) or host.get('CpuShares',0) not in (0,1024)
            or host.get('CpusetCpus') or host.get('CpusetMems') or host.get('CpuRealtimeRuntime')
            or host.get('MemoryReservation') or host.get('MemorySwappiness') is not None
            or host.get('OomKillDisable') or host.get('PidsLimit') not in ((512,) if guard else (None,0,-1))):
        fail(f'rank {rank}: unsupported additional resource constraints')
    if guard and (host.get('CgroupnsMode') != 'private' or not host.get('AutoRemove')
                  or not config.get('OpenStdin') or host.get('PidMode')):
        fail(f'rank {rank}: guard lifetime or cgroup isolation differs')
    expected_swap=c['memory_limit_bytes']*(1 if guard else 2)
    if expected_swap:
        if observed['memory_swap_limit_bytes']!=expected_swap:
            fail(f'rank {rank}: memory swap limit differs')
    elif observed['memory_swap_limit_bytes'] not in (0,-1):
        fail(f'rank {rank}: unexpected memory swap limit')
    for key in ("network_mode", "ipc_mode", "shm_size_bytes", "memory_limit_bytes", "cpu_limit_nanos",
                "restart_policy", "restart_max_retries"):
        if observed[key] != c[key]:
            fail(f"rank {rank}: container {key} differs")
    if (observed['ulimits'] != c['ulimits']
            or len(host.get('Ulimits') or []) != len(observed['ulimits'])):
        fail(f'rank {rank}: container ulimits differ')
    requests = observed["accelerator_requests"]
    if (len(requests) != 1 or requests[0].get("Count") != -1 or requests[0].get("DeviceIDs")
            or not any("gpu" in capabilities for capabilities in requests[0].get("Capabilities") or [])):
        fail(f"rank {rank}: accelerator access differs")
    devices = observed["devices"]
    if c["devices"]:
        if not devices or any(not (d.get("PathOnHost") == '/dev/infiniband' or str(d.get("PathOnHost", "")).startswith("/dev/infiniband/"))
                              or d.get("PathOnHost") != d.get("PathInContainer")
                              or d.get("CgroupPermissions") != "rwm" for d in devices):
            fail(f"rank {rank}: required device access differs")
    elif devices:
        fail(f"rank {rank}: unexpected device access")
    ports = observed["port_bindings"]
    if c["network_mode"] == "bridge":
        rows = ports.get(str(plan["port"]) + "/tcp")
        if set(ports) != {str(plan["port"]) + "/tcp"} or not isinstance(rows, list) or len(rows) != 1 or rows[0].get("HostPort") != str(plan["port"]) or rows[0].get("HostIp", "") not in ("", "0.0.0.0"):
            fail(f"rank {rank}: port bindings differ")
    elif ports:
        fail(f"rank {rank}: unexpected port bindings")
    health = c["healthcheck"]
    if health:
        expected_health = {"Test": ["CMD-SHELL", f"curl -fs http://localhost:{plan['port']}{health['path']} || exit 1"],
            "Interval": health["interval_seconds"] * 1000000000, "Timeout": health["timeout_seconds"] * 1000000000,
            "Retries": health["retries"], "StartPeriod": health["start_period_seconds"] * 1000000000}
        if any(observed["healthcheck"].get(key) != value for key, value in expected_health.items()):
            fail(f"rank {rank}: healthcheck differs")
    elif observed["healthcheck"].get("Test") != ["NONE"]:
        fail(f"rank {rank}: image healthcheck was not explicitly disabled")
    witness = canonical_json_digest({"container_id": text(container.get("Id"), "container ID"),
                                    "started_at": text(state.get("StartedAt"), "container start time")})
    # Never return a credential in normalized command/configuration evidence.
    observed["command"] = command
    observed['image_digest'] = recipe['image_digest']
    observed['environment'] = {name: '<credential>' if re.search(
        r'(?i)(?:^|_)(?:TOKEN|PASSWORD|SECRET|CREDENTIAL|API_KEY|AUTHORIZATION)(?:_|$)',name) else value
        for name,value in actual.items()}
    observed['model_mounts'] = [{'source':item['Source'],'target':item['Destination'],'read_only':not item['RW']}
                                 for item in matching]
    portable = {key: observed[key] for key in ('network_mode','ipc_mode','shm_size_bytes',
        'memory_limit_bytes','cpu_limit_nanos','restart_policy','restart_max_retries')}
    portable['ulimits'] = observed['ulimits']
    portable['accelerator_access'] = 'all'  # All-device request was checked above.
    portable['devices'] = ['infiniband'] if devices else []
    if guard:
        portable['guard'] = copy.deepcopy(guard)
    if health:
        from urllib.parse import urlparse
        raw_health=observed['healthcheck']
        portable['healthcheck'] = {'path':urlparse(raw_health['Test'][1].split()[2]).path,
            'interval_seconds':raw_health['Interval']//1000000000,
            'timeout_seconds':raw_health['Timeout']//1000000000,
            'retries':raw_health['Retries'],'start_period_seconds':raw_health['StartPeriod']//1000000000}
    else:
        portable['healthcheck']=None
    return {"rank": rank, "running": True, "owned": True, "spec_id": plan["spec_id"],
            "image_digest": recipe["image_digest"], **({'snapshots':{name:{'snapshot_manifest_id':item['snapshot_manifest']['manifest_id']}
                for name,item in serving.required_snapshots(plan['spec']).items()}} if plan['spec']['schema_version']==3
                else {'snapshot_manifest_id':recipe['model']['snapshot_manifest']['manifest_id']}),
            "boot_witness": witness, "container_configuration": observed,
            'public_container_configuration': portable}
