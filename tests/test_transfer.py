"""Manifest sharding, confirmed transport and guarded staging tests."""
import base64
import copy
import hashlib
import itertools
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from release_spec import build_snapshot_manifest
from model_library.transfer import partition_files, selected_rail_between, validate_ssh_roce_route, transfer_map, STAGING_CHECK_CODE, stream_command
from scripts import topology_manifest as tm


def topology(nodes):
    rows=[]
    for rank in range(nodes):
        algorithm=b'ssh-ed25519';raw=len(algorithm).to_bytes(4,'big')+algorithm+f'fixture-public-key-{rank}'.encode()
        key=base64.b64encode(raw).decode()
        rows.append(dict(rank=rank,node_id=f'node-{rank}',hostname=f'rank-{rank}',ssh_host='local' if rank==0 else f'alias-{rank}',control=dict(interface='mgmt0',ip=f'192.0.2.{10+rank}'),gpu='NVIDIA GB10',rdma=[dict(hca='roce0',netdev='fabric0',cidrs=[f'198.51.100.{10+rank}/24'])],ssh_host_keys=[dict(algorithm='ssh-ed25519',public_key=key,fingerprint=tm.host_key_fingerprint(key))]))
    links=[dict(ranks=[a,b],rails=[dict(network='198.51.100.0/24',a=dict(hca='roce0',netdev='fabric0',ip=f'198.51.100.{10+a}'),b=dict(hca='roce0',netdev='fabric0',ip=f'198.51.100.{10+b}'))]) for a,b in itertools.combinations(range(nodes),2)]
    doc=dict(schema_version=2,nodes=rows,links=links,validation=dict(full_mesh=True,connectivity_verified=True,min_rails_per_pair=1,ssh_identity_enrolled=True))
    doc['validation']['class']='roce-full-mesh'
    doc['topology_id']=tm.topology_digest(doc);tm.validate_manifest(doc,require_verified=True)
    return doc


def manifest_for(files):
    return build_snapshot_manifest(model_id='example/model',snapshot_revision='a'*40,files=[dict(path=p,size=len(data),sha256=hashlib.sha256(data).hexdigest()) for p,data in files.items()])


class TransferPlan(unittest.TestCase):
    def test_balanced_manifest_streams_cover_files_once(self):
        files={f'part-{i}.bin':bytes(i+1) for i in range(20)}
        groups=partition_files(manifest_for(files))
        self.assertEqual(len(groups),8)
        flattened=[path for group in groups for path in group['files']]
        self.assertEqual(sorted(flattened),sorted(files));self.assertEqual(len(flattened),len(set(flattened)))
        self.assertLessEqual(max(g['bytes'] for g in groups)-min(g['bytes'] for g in groups),20)

    def test_literal_punctuation_is_not_shell_code(self):
        names={'dir/$(touch BAD); quote\' space.bin':b'a','--help':b'b'}
        self.assertEqual(sorted(path for group in partition_files(manifest_for(names)) for path in group['files']),sorted(names))

    def test_escape_and_control_names_rejected(self):
        for name in ('../escape','/absolute','x\ny','x\ty','a//b','a/./b'):
            with self.subTest(name=name),self.assertRaises(ValueError):partition_files(manifest_for({name:b'x'}))

    def test_pair_selection_for_two_and_three_nodes(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'topology.json'
            for nodes in (2,3):
                doc=topology(nodes);path.write_text(json.dumps(doc))
                for rank in range(1,nodes):
                    mapped=transfer_map(path,rank,0)
                    self.assertEqual(mapped['control_ssh_host'],f'alias-{rank}')
                    self.assertEqual(mapped['remote_ip'],f'198.51.100.{10+rank}')
            with self.assertRaisesRegex(ValueError,'two-remote'):transfer_map(path,1,2)

    def test_route_mismatch(self):
        good=[dict(dev='fabric0',prefsrc='198.51.100.10')]
        kw=dict(remote_ip='198.51.100.11',expected_netdev='fabric0',expected_source_ip='198.51.100.10')
        validate_ssh_roce_route(good,**kw)
        for value in ([],[{}],[dict(dev='mgmt0',prefsrc='198.51.100.10')],[dict(dev='fabric0',prefsrc='192.0.2.10')],good*2):
            with self.assertRaises(ValueError):validate_ssh_roce_route(value,**kw)

    @unittest.skipUnless(shutil.which('rsync'), 'rsync not installed')
    def test_real_rsync_literal_file_list(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source';dest=root/'dest';source.mkdir();dest.mkdir()
            name="$(touch BAD); quote' space.bin"
            (source/name).write_bytes(b'literal')
            listing=root/'files.list';listing.write_bytes(name.encode()+b'\0')
            result=subprocess.run(['rsync','-rt','--protect-args','--from0',f'--files-from={listing}','--',str(source)+'/',str(dest)+'/'],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual((dest/name).read_bytes(),b'literal')
            self.assertFalse((root/'BAD').exists())

    def test_staging_must_be_empty_private_operation_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);dest=root/('.pending-'+'b'*32)/'snapshots'/('a'*40);dest.mkdir(parents=True)
            def run():return subprocess.run([sys.executable,'-c',STAGING_CHECK_CODE,str(dest),'a'*40],capture_output=True).returncode
            self.assertEqual(run(),0)
            (dest/'existing').write_text('x');self.assertNotEqual(run(),0)
            (dest/'existing').unlink();dest.rmdir();dest.symlink_to(root,target_is_directory=True);self.assertNotEqual(run(),0)


class TransferShell(unittest.TestCase):
    def scenario(self,nodes=2,mode='ok',pull=False,relay=False):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);top=topology(nodes);(root/'topology.json').write_text(json.dumps(top))
            source=root/'source';source.mkdir()
            contents={f'part-{i}.bin':str(i).encode()*10 for i in range(12)}
            contents["dir/$(touch BAD); quote' space.bin"]=b'literal'
            for name,data in contents.items():
                path=source/name;path.parent.mkdir(exist_ok=True);path.write_bytes(data)
            manifest=manifest_for(contents);(root/'manifest.json').write_text(json.dumps(manifest))
            dest=root/('.pending-'+'b'*32)/'snapshots'/('a'*40);dest.mkdir(parents=True)
            mock=root/'rsync.py';mock.write_text('''#!/usr/bin/env python3
import json,os,pathlib,shutil,sys,time
root=pathlib.Path(os.environ['TRANSFER_FIXTURE']);args=sys.argv[1:]
listfile=pathlib.Path(next(a.split('=',1)[1] for a in args if a.startswith('--files-from=')))
(root/(listfile.stem+'.argv')).write_text(json.dumps(args))
if os.environ['TRANSFER_MODE']=='stream-failure' and listfile.stem=='stream-2':sys.exit(23)
source,dest=args[-2:]
if not source.startswith('/'):source=source.split(':',1)[1]
if not dest.startswith('/'):dest=dest.split(':',1)[1]
for name in listfile.read_bytes().split(b'\\0'):
 if not name:continue
 target=pathlib.Path(dest)/name.decode();target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(pathlib.Path(source)/name.decode(),target)
''');mock.chmod(0o755)
            ip=root/'ip.py';ip.write_text('''#!/usr/bin/env python3
import json,os
print(json.dumps([dict(dev='mgmt0' if os.environ['TRANSFER_MODE']=='local-route' else 'fabric0',prefsrc='198.51.100.10')]))
''');ip.chmod(0o755)
            ssh=root/'ssh.py';ssh.write_text("#!/usr/bin/env python3\nimport base64,json,os,pathlib,shlex,subprocess,sys,uuid\nroot=pathlib.Path(os.environ['TRANSFER_FIXTURE']);args=sys.argv[1:]\n(root/('ssh-'+uuid.uuid4().hex+'.argv')).write_text(json.dumps(args))\ncommand=shlex.split(args[-1]);mode=command[-3]\nfiles=json.loads(base64.b64decode(command[-1]))\nif os.environ['TRANSFER_MODE']=='sender-failure' and mode=='send' and any(f['path']=='part-2.bin' for f in files):sys.exit(23)\nif os.environ['TRANSFER_MODE']=='receiver-failure' and mode=='receive' and any(f['path']=='part-2.bin' for f in files):sys.exit(23)\nraise SystemExit(subprocess.run(command).returncode)\n");ssh.chmod(0o755)
            remote=nodes-1
            body=f'''
set -euo pipefail
. {shlex.quote(str(ROOT/'scripts/lib.sh'))}
. {shlex.quote(str(ROOT/'scripts/model-library-common.sh'))}
. {shlex.quote(str(ROOT/'scripts/model-transfer.sh'))}
CLUSTER_TOPOLOGY_FILE={shlex.quote(str(root/'topology.json'))}
CLUSTER_TOPOLOGY_ID={top['topology_id']}
CLUSTER_NODE_SSH_HOSTS=(local alias-1 alias-2)
require_topology_ssh_trust() {{ [ "$TRANSFER_MODE" != no-trust ]; }}
if [ "$TRANSFER_MODE" = wrong-alias ]; then CLUSTER_NODE_SSH_HOSTS[{remote}]=other; fi
model_node() {{ printf '%s' "$2" | python3 -m model_library.node; }}
ssh_node() {{
  if [[ "$2" = ip* ]]; then
    if [ "$TRANSFER_MODE" = remote-route ]; then printf '[{{"dev":"mgmt0","prefsrc":"198.51.100.%s"}}]\\n' "$((10+$1))"; else printf '[{{"dev":"fabric0","prefsrc":"198.51.100.%s"}}]\\n' "$((10+$1))"; fi
  else bash -c "$2"; fi
}}
model_transfer {1 if relay else remote if pull else 0} {shlex.quote(str(source))} {0 if pull else remote} {shlex.quote(str(dest))} "$(cat {shlex.quote(str(root/'manifest.json'))})"
'''
            env={**os.environ,'TRANSFER_FIXTURE':str(root),'TRANSFER_MODE':mode,'PULSAR_RSYNC':str(mock),'PULSAR_IP':str(ip),'PULSAR_SSH':str(ssh) if relay else 'ssh'}
            result=subprocess.run(['bash','-c',body],env=env,cwd=ROOT,text=True,capture_output=True)
            argv=[json.loads(p.read_text()) for p in root.glob('ssh-*.argv' if relay else 'stream-*.argv')]
            return result,argv,sorted(str(p.relative_to(dest)) for p in dest.rglob('*') if p.is_file()),sorted(contents)

    def test_push_pull_and_three_nodes(self):
        for nodes,pull in ((2,False),(2,True),(3,False)):
            result,argv,actual,expected=self.scenario(nodes,pull=pull)
            self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(actual,expected);self.assertEqual(len(argv),8)
            for args in argv:
                shell=args[args.index('-e')+1]
                self.assertIn(f'HostName=198.51.100.{10+nodes-1}',shell)
                self.assertIn(f'HostKeyAlias=alias-{nodes-1}',shell)
                self.assertIn('BindAddress=198.51.100.10',shell)
                self.assertNotIn('--delete',args)

    def test_two_remote_relay_uses_pipes_and_both_confirmed_identities(self):
        result,argv,actual,expected=self.scenario(3,relay=True)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(actual,expected)
        self.assertEqual(len(argv),16)
        aliases={args[args.index('--')+1] for args in argv}
        self.assertEqual(aliases,{'alias-1','alias-2'})
        for args in argv:
            alias=args[args.index('--')+1];rank=int(alias.rsplit('-',1)[1])
            self.assertIn(f'HostName=198.51.100.{10+rank}',args)
            self.assertIn(f'HostKeyAlias={alias}',args)
        self.assertIn('controller stored no model files',result.stderr)

    def test_relay_pipeline_failure_is_not_success(self):
        for mode in ('sender-failure','receiver-failure','remote-route'):
            with self.subTest(mode=mode):
                result,_,_,_=self.scenario(3,mode=mode,relay=True)
                self.assertNotEqual(result.returncode,0,result.stdout)

    def test_routes_identity_and_stream_failure(self):
        for mode in ('local-route','remote-route','wrong-alias','no-trust','stream-failure'):
            with self.subTest(mode=mode):
                result,argv,_,_=self.scenario(mode=mode)
                self.assertNotEqual(result.returncode,0,result.stdout)
                if mode!='stream-failure':self.assertEqual(argv,[])


if __name__=='__main__':unittest.main()
