"""Whole-record observer I/O and argv-derived Docker fixtures, CPU only.

No product test switch: a test-owned Python executable injects these I/O doubles
before invoking the actual modules. Unsupported external operations are errors.
"""
import base64
import copy
import json
import os
from pathlib import Path
import signal
import resource
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BOOT = "c" * 32
IMAGE = "sha256:" + "a" * 64
CID = "d" * 64


def start_ticks(pid):
    try:
        return Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()[19]
    except (OSError, IndexError):
        return None


def register(root, role):
    with (root/'owned.jsonl').open('a') as f:
        f.write(json.dumps({'pid':os.getpid(),'start':start_ticks(os.getpid()),'role':role})+'\n')


def save(path, value):
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value))
    os.replace(tmp,path)


def scenario(root):
    return json.loads((root/'scenario.json').read_text())


def shim_main():
    args=sys.argv[1:]
    clean=[a for a in args if a not in ('-I','-B')]
    root=Path(os.environ['DIAG_FIXTURE'])
    target=clean[0] if clean else ''
    if target in (str(ROOT/'scripts/diagnostic_cli.py'),str(ROOT/'scripts/diagnostic_kmsg.py')):
        from scripts import diagnostic_kmsg as native
        native.BOOT_PATH=str(root/'boot')
        native.MEMINFO_PATH=str(root/'meminfo')
        native.PROC_ROOT=root/'proc'
        native.CGROUP_ROOT=root/'cgroup'
        spec=scenario(root)
        if target.endswith('diagnostic_kmsg.py'):
            register(root,'observer')
            opened=[]
            def open_kmsg():
                if spec.get('deny_kmsg'):
                    raise PermissionError('synthetic kmsg denied')
                fd=os.open(root/'kernel-queue.jsonl',os.O_RDONLY|os.O_NONBLOCK)
                opened.append(fd)
                with (root/'kernel-io.jsonl').open('a') as f:
                    f.write(json.dumps({'event':'open-tail','fd':fd})+'\n')
                return fd
            pending=bytearray()
            count=0
            def read_one(fd):
                nonlocal count
                if spec.get('observer_death_after_start') and (root/'started').exists():os._exit(9)
                if spec.get('flood') and (not spec.get('flood_after_start') or (root/'started').exists()) and (not spec.get('flood_after_remove') or (root/'removed').exists()):
                    count+=1
                    return 'record',native.parse_kmsg_record(f'6,{count},{count},-;synthetic flood\n'.encode())
                while b'\n' not in pending:
                    data=os.read(fd,8192)
                    if not data:
                        return 'eagain',None # Explicit injected whole-record EAGAIN.
                    pending.extend(data)
                line,_,rest=pending.partition(b'\n')
                pending[:]=rest
                row=json.loads(line)
                if row['status']=='record':
                    count+=1
                    return 'record',native.decode_record(base64.b64decode(row['data']))
                return row['status'],None
            native.open_kmsg=open_kmsg
            native.read_one=read_one
            if spec.get('observer_death'):
                os._exit(9)
            return native.main(clean[1:])
        from scripts import diagnostic_cli
        command=clean[1] if len(clean)>1 else ''
        if spec.get('helper_failure')==command:
            return 17
        return diagnostic_cli.main(clean[1:])
    # Reuse real topology schema/trust checking with synthetic local probes.
    if target==str(ROOT/'scripts/topology_ssh_trust.py') and '--probe' in args:
        args[args.index('--probe')+1]=str(root/'topo/local-identity-probe.py')
    if target==str(ROOT/'scripts/probe-node.py'):
        print((root/'topo/probe-0.json').read_text())
        return 0
    os.execv(sys.executable,[sys.executable,*args])


def parse_create(argv):
    """Docker defaults are neutral; each requested non-default is parsed."""
    host={'Privileged':False,'AutoRemove':False,'ReadonlyRootfs':False,'NetworkMode':'default','Runtime':'runc',
          'Memory':0,'MemorySwap':0,'NanoCpus':0,'PidsLimit':None,'CapDrop':None,'SecurityOpt':None,
          'IpcMode':'private','ShmSize':67108864,'Tmpfs':None,'RestartPolicy':{'Name':'no','MaximumRetryCount':0},
          'LogConfig':{'Type':'json-file','Config':{}},'DeviceRequests':None,'CapAdd':None,'Devices':[],
          'DeviceCgroupRules':None,'Binds':None,'VolumesFrom':None,'Sysctls':None,'ExtraHosts':None,'PortBindings':{},
          'PublishAllPorts':False,'PidMode':'','UTSMode':'','UsernsMode':'','CgroupnsMode':'private','CgroupParent':'',
          'OomKillDisable':False,'CpuQuota':0,'CpuPeriod':0,'CpusetCpus':'','CpusetMems':'','StorageOpt':None,'Init':False,
          'MaskedPaths':['/proc/acpi','/proc/asound','/proc/interrupts','/proc/kcore','/proc/keys','/proc/latency_stats',
                         '/proc/timer_list','/proc/timer_stats','/proc/sched_debug','/proc/scsi','/sys/firmware','/sys/devices/virtual/powercap'],
          'ReadonlyPaths':['/proc/bus','/proc/fs','/proc/irq','/proc/sys','/proc/sysrq-trigger']}
    config={'User':'','WorkingDir':'','Entrypoint':None,'Cmd':None,'Env':[],'Labels':{},'Healthcheck':None,'Volumes':None}
    mounts=[]
    name=None
    args=iter(argv[1:])
    for flag in args:
        if flag.startswith('sha256:'):
            config['Cmd']=list(args)
            image=flag
            break
        if flag=='--read-only':host['ReadonlyRootfs']=True;continue
        if flag=='--no-healthcheck':config['Healthcheck']={'Test':['NONE']};continue
        value=next(args)
        direct={'--network':'NetworkMode','--runtime':'Runtime','--ipc':'IpcMode'}
        numbers={'--memory':'Memory','--memory-swap':'MemorySwap','--pids-limit':'PidsLimit','--shm-size':'ShmSize'}
        if flag in direct:host[direct[flag]]=value
        elif flag in numbers:host[numbers[flag]]=int(value)
        elif flag=='--name':name=value
        elif flag=='--pull':assert value=='never'
        elif flag=='--cpus':host['NanoCpus']=int(float(value)*10**9)
        elif flag=='--gpus':
            assert value.startswith('device=')
            host['DeviceRequests']=[{'Driver':'','Count':0,'DeviceIDs':value[7:].split(','),'Capabilities':[['gpu']],'Options':{}}]
        elif flag=='--cap-drop':host['CapDrop']=(host['CapDrop'] or [])+[value]
        elif flag=='--security-opt':host['SecurityOpt']=(host['SecurityOpt'] or [])+[value]
        elif flag=='--tmpfs':
            dest,options=value.split(':',1)
            host['Tmpfs']={**(host['Tmpfs'] or {}),dest:options}
        elif flag=='--restart':host['RestartPolicy']['Name']=value
        elif flag=='--log-driver':host['LogConfig']['Type']=value
        elif flag=='--log-opt':
            k,v=value.split('=',1);host['LogConfig']['Config'][k]=v
        elif flag=='--entrypoint':config['Entrypoint']=[value]
        elif flag=='--stop-signal':config['StopSignal']=value
        elif flag=='--stop-timeout':config['StopTimeout']=int(value)
        elif flag=='--workdir':config['WorkingDir']=value
        elif flag=='--env':config['Env'].append(value)
        elif flag=='--label':
            k,v=value.split('=',1);config['Labels'][k]=v
        elif flag=='--mount':
            options=value.split(',')
            fields=dict(s.split('=',1) for s in options if '=' in s)
            mounts.append({'Type':fields.get('type'),'Source':fields.get('src'),'Destination':fields.get('dst'),
                           'RW':'readonly' not in options,'Propagation':fields.get('bind-propagation'),'Mode':''})
        else:raise AssertionError('unsupported create flag '+flag)
    return {'Id':CID,'Image':image,'Name':'/'+name,'Path':config['Entrypoint'][0],'Args':config['Cmd'],'HostConfig':host,'Config':config,'Mounts':mounts,
            'State':{'Status':'created','Running':False,'Restarting':False,'Pid':0,'ExitCode':0,'OOMKilled':False,'Error':'',
                     'StartedAt':'0001-01-01T00:00:00Z','FinishedAt':'0001-01-01T00:00:00Z'}}


def kernel(root, records):
    with (root/'kernel-queue.jsonl').open('a') as f:
        for record in records:
            row=record if isinstance(record,dict) else {'status':'record','data':base64.b64encode(record.encode()).decode()}
            f.write(json.dumps(row)+'\n')


def workload_main(root):
    # A Docker daemon does not inherit a client's capture rlimit. Restore that
    # process boundary in this child double; the compiled entrypoint then sets
    # its own hard per-step file limit before executing the actual payload.
    _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    resource.setrlimit(resource.RLIMIT_FSIZE, (hard, hard))
    register(root,'synthetic-workload')
    item=json.loads((root/'docker.json').read_text())
    bind=item['Mounts'][0]['Source']
    source=Path(bind,'pulsar-diagnostic-entrypoint.sh').read_text()
    scratch=root/'scratch';scratch.mkdir(exist_ok=True)
    # The fixture maps container paths into its private temporary filesystem.
    # It executes the actual compiled fail-first/time/output-control script.
    source=source.replace('/pulsar-check/',bind+'/').replace('/tmp/pulsar-step.',str(scratch/'pulsar-step.')).replace('/tmp/result.json',str(scratch/'result.json'))
    script=root/'entrypoint.sh';script.write_text(source)
    env=dict(s.split('=',1) for s in item['Config']['Env'])
    child=subprocess.Popen([*item['Config']['Entrypoint'],*item['Config']['Cmd'][:-1],str(script)],
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,start_new_session=True)
    with (root/'owned.jsonl').open('a') as f:
        f.write(json.dumps({'pid':child.pid,'start':start_ticks(child.pid),'role':'entrypoint'})+'\n')
    def stop(sig,frame):
        if child.poll() is None:
            os.kill(child.pid,signal.SIGTERM)
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    (root/'workload-ready').write_text('ready')
    stdout,stderr=child.communicate(timeout=35)
    rc=child.returncode
    (root/'workload.log').write_bytes(stdout+stderr)
    save(root/'workload-exit.json',{'exit':rc if rc>=0 else 128-rc})
    return 0


def docker_main():
    root=Path(os.environ['DIAG_FIXTURE'])
    register(root,'docker-client')
    args=sys.argv[1:]
    with (root/'docker-calls.jsonl').open('a') as f:f.write(json.dumps({'argv':args,'ns':time.monotonic_ns()})+'\n')
    spec=scenario(root)
    cmd=args[0]
    if spec.get('client_orphan_after') == cmd:
        pid=os.fork()
        if pid == 0:
            signal.signal(signal.SIGTERM,signal.SIG_IGN)
            register(root,'client-orphan')
            time.sleep(20)
            os._exit(0)
    if spec.get('hang')==cmd:
        time.sleep(30)
    if cmd=='info':print('Runtimes: runc nvidia');return 0
    if cmd=='ps':
        if spec.get('busy'):print('synthetic-busy')
        return 0
    if args[:2]==['image','inspect']:
        if spec.get('floor_after_admission'):
            (root/'meminfo').write_text('MemAvailable: 1048576 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n')
        print(json.dumps([spec.get('image_document',{'Id':IMAGE,'Os':'linux','Architecture':'arm64','Config':{'Env':[],'User':'','Volumes':None}})]));return 0
    state_path=root/'docker.json'
    item=json.loads(state_path.read_text()) if state_path.exists() else None
    def persist():save(state_path,item)
    if cmd=='create':
        assert item is None,'second create'
        item=parse_create(args)
        if spec.get('image_document'):
            inherited = dict(v.split('=',1) for v in spec['image_document']['Config']['Env'])
            inherited.update(dict(v.split('=',1) for v in item['Config']['Env']))
            item['Config']['Env']=[k+'='+v for k,v in inherited.items()]
        persist()
        (root/'created').write_text('1')
        if spec.get('hang_after_create'):
            time.sleep(30)
        if spec.get('mutate_caller'):
            (root/'inputs/step.py').write_text('caller was changed\n')
        if spec.get('mutate_sealed'):
            target=Path(item['Mounts'][0]['Source'])/'step.py'
            target.chmod(0o644);target.write_text('sealed mutation\n')
        if spec.get('create_reply'):
            return int(spec['create_reply'])
        print(CID);return 0
    if cmd=='inspect':
        if spec.get('query_failure'):return 1
        if item is None:
            print('No such container',file=sys.stderr);return 1
        if args[-1] not in (CID,item['Name'].lstrip('/')):raise AssertionError('foreign inspect target')
        if item['State']['Running'] and (root/'workload-exit.json').exists():
            item['State'].update(Status='exited',Running=False,Pid=0,ExitCode=json.loads((root/'workload-exit.json').read_text())['exit'],FinishedAt='2026-09-19T00:00:02Z')
            persist()
        observed=copy.deepcopy(item)
        for path,value in spec.get('inspect_patch',{}).items():
            target=observed
            parts=path.split('.',2)
            for part in parts[:-1]:target=target[part]
            if value=='__omit__':target.pop(parts[-1],None)
            else:target[parts[-1]]=value
        if spec.get('reject_identity_after_start') and (root/'started').exists():
            observed['Config']['Labels']['io.pulsar.diagnostic.attempt-nonce']='foreign'
        if spec.get('empty_stopped') and not observed['State'].get('Running') and (root/'started').exists():observed['State']={}
        print(json.dumps([observed]));return 0
    if cmd=='start':
        assert args==['start',CID] and item['State']['Status']=='created'
        (root/'started').write_text('1')
        item['State'].update(Status='running',Running=True,Pid=4242,StartedAt='2026-09-19T00:00:00Z')
        persist()
        child=subprocess.Popen([sys.executable,'-I','-B',str(Path(__file__).resolve()),'workload',str(root)],
                               stdout=subprocess.DEVNULL,stderr=(root/'workload-supervisor.stderr').open('wb'),start_new_session=True)
        save(root/'workload-pid.json',{'pid':child.pid,'start':start_ticks(child.pid)})
        if spec.get('hang_after_start'):
            time.sleep(30)
        if spec.get('start_reply'):return int(spec['start_reply'])
        return 0
    if cmd=='stop':
        assert args==['stop','--timeout','-1',CID]
        if spec.get('stop_failure'):return 1
        pid_path=root/'workload-pid.json'
        if pid_path.exists():
            owned=json.loads(pid_path.read_text())
            if start_ticks(owned['pid'])==owned['start']:
                os.kill(owned['pid'],signal.SIGTERM)
            until=time.monotonic()+1
            while not (root/'workload-exit.json').exists() and time.monotonic()<until:time.sleep(.01)
        if pid_path.exists() and not (root/'workload-exit.json').exists():
            return 1  # A sent signal is not an actual stopped state.
        code=json.loads((root/'workload-exit.json').read_text())['exit'] if pid_path.exists() else 0
        item['State'].update(Status='exited',Running=False,Pid=0,ExitCode=code,FinishedAt='2026-09-19T00:00:02Z')
        persist();kernel(root,spec.get('kernel_on_stop',[]));return 0
    if cmd=='logs':
        assert args==['logs',CID]
        if spec.get('logs_failure'):return 1
        sys.stdout.write((root/'workload.log').read_text() if (root/'workload.log').exists() else '')
        return 0
    if cmd=='rm':
        assert args==['rm',CID] and item['State']['Running'] is False
        kernel(root,spec.get('kernel_on_remove',[]))
        if spec.get('journal_on_remove'):
            with (root/'journal-records.jsonl').open('a') as stream:
                for i,message in enumerate(spec['journal_on_remove'],1):
                    stream.write(json.dumps(journal_record(i,message))+'\n')
        (root/'removed').write_text('1')
        if not spec.get('rm_remains'):state_path.unlink()
        return 1 if spec.get('rm_failure') else 0
    if args[:2]==['container','ls']:
        assert args[2:4]==['--all','--no-trunc'] and args[-2:]==['--format','{{json .ID}}']
        assert args[4]=='--filter' and (args[5]=='id='+CID or args[5].startswith('name=^/pulsar-diag-'))
        if spec.get('absence_failure'):return 1
        if item:print(json.dumps(item['Id']))
        return 0
    raise AssertionError('unsupported external Docker operation '+str(args))


def journal_record(index, message):
    return {'_BOOT_ID':BOOT,'_TRANSPORT':'kernel','__CURSOR':'s=fixture;i='+str(index),
            '__MONOTONIC_TIMESTAMP':str(time.monotonic_ns()//1000),
            '__REALTIME_TIMESTAMP':str(time.time_ns()//1000),'MESSAGE':message}


def journal_main():
    root=Path(os.environ['DIAG_FIXTURE'])
    register(root,'journal-client')
    args=sys.argv[1:]
    with (root/'journal-calls.jsonl').open('a') as stream:
        stream.write(json.dumps({'argv':args,'ns':time.monotonic_ns(),'pid':os.getpid()})+'\n')
    allowed={'--system','--dmesg','--boot='+BOOT,'--no-pager','--all','--output=json',
             '--output-fields=_BOOT_ID,_TRANSPORT,__CURSOR,__MONOTONIC_TIMESTAMP,__REALTIME_TIMESTAMP,MESSAGE',
             '--lines=1','--follow','--no-tail','--cursor=s=fixture;i=0'}
    assert all(arg in allowed for arg in args),args
    rows=(root/'journal-records.jsonl').read_text().splitlines()
    if '--lines=1' in args:
        print(rows[-1],flush=True);return 0
    assert '--cursor=s=fixture;i=0' in args and '--no-tail' in args
    if '--follow' not in args:
        print('\n'.join(rows),flush=True);return 0
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
    count=0
    while True:
        rows=(root/'journal-records.jsonl').read_text().splitlines()
        if scenario(root).get('journal_queue_until_final'):
            rows=rows[:1]
        for row in rows[count:]:print(row,flush=True)
        count=len(rows)
        time.sleep(.01)


if __name__=='__main__':
    sys.path.insert(0,str(ROOT))
    if sys.argv[1:2]==['workload']:
        raise SystemExit(workload_main(Path(sys.argv[2])))
    raise SystemExit(docker_main())
