"""Deterministic snapshot stream plans and exact confirmed-rail checks.

No transport executes here. Bash selects the existing SSH identity and owns the
processes. Streams copy the canonical flat snapshot file set, never Hub blobs.
"""
import argparse
import json
from pathlib import Path, PurePosixPath
import sys
from release_spec import verify_snapshot_manifest
from .integrity import StorageError


def selected_rail_between(topology, first, second, rail_index=0):
    if type(first) is not int or type(second) is not int or first==second:
        raise StorageError('transfer requires two different confirmed ranks')
    if type(rail_index) is not int or rail_index<0:
        raise StorageError('rail index must be nonnegative')
    wanted=[min(first,second),max(first,second)]
    link=next((link for link in topology['links'] if link['ranks']==wanted),None)
    if link is None:raise StorageError('no confirmed fabric link between transfer endpoints')
    rails=sorted(link['rails'],key=lambda item:(item['network'],item['a']['ip'],item['b']['ip']))
    if rail_index>=len(rails):raise StorageError('selected confirmed rail does not exist')
    rail=rails[rail_index]
    first_side='a' if first<second else 'b'
    second_side='b' if first<second else 'a'
    return rail[first_side],rail[second_side],rail['network']


def validate_ssh_roce_route(route_data, *, remote_ip, expected_netdev, expected_source_ip):
    if not remote_ip or not expected_netdev or not expected_source_ip:
        raise StorageError('route check requires peer, interface and source address')
    if not isinstance(route_data,list) or len(route_data)!=1 or not isinstance(route_data[0],dict):
        raise StorageError('route query did not return one unambiguous route')
    route=route_data[0]
    if route.get('dev')!=expected_netdev:
        raise StorageError('route interface differs from the confirmed RoCE rail')
    if (route.get('prefsrc') or route.get('src'))!=expected_source_ip:
        raise StorageError('route source address differs from the confirmed RoCE rail')
    if route.get('type','unicast')!='unicast':
        raise StorageError('selected RoCE route is not unicast')
    return dict(remote_ip=remote_ip,netdev=expected_netdev,source_ip=expected_source_ip)


def partition_files(manifest, streams=8):
    manifest=verify_snapshot_manifest(manifest)
    if type(streams) is not int or not 1<=streams<=8:raise StorageError('transfer uses at most eight streams')
    for item in manifest['files']:
        path=item['path']
        if any(ord(c)<32 or ord(c)==127 for c in path):raise StorageError('snapshot filenames may not contain control characters')
        if str(PurePosixPath(path))!=path or path in ('.',''):
            raise StorageError('snapshot filenames must have canonical relative spelling')
    groups=[dict(stream=i,bytes=0,files=[]) for i in range(min(streams,len(manifest['files'])))]
    for item in sorted(manifest['files'],key=lambda f:(-f['size'],f['path'])):
        group=min(groups,key=lambda g:(g['bytes'],g['stream']))
        group['files'].append(item['path']);group['bytes']+=item['size']
    for group in groups:group['files'].sort()
    return groups


def transfer_map(topology_path,source_rank,destination_rank):
    from scripts.topology_manifest import load_json, extract_topology, validate_manifest, topology_has_ssh_trust
    topology=extract_topology(load_json(topology_path));validate_manifest(topology,require_verified=True)
    if not topology_has_ssh_trust(topology):raise StorageError('transfer requires enrolled topology SSH identities')
    if source_rank==destination_rank or (source_rank!=0 and destination_rank!=0):
        raise StorageError('transfer supports one local and one remote endpoint; two-remote transfer requires explicit orchestration')
    remote=max(source_rank,destination_rank)
    if min(source_rank,destination_rank)<0 or remote>=len(topology['nodes']):raise StorageError('transfer endpoint is not confirmed')
    local_rail,remote_rail,network=selected_rail_between(topology,0,remote)
    return dict(topology_id=topology['topology_id'],remote_rank=remote,
                control_ssh_host=topology['nodes'][remote]['ssh_host'],
                local_ip=local_rail['ip'],local_netdev=local_rail['netdev'],
                remote_ip=remote_rail['ip'],remote_netdev=remote_rail['netdev'],network=network)


# This small filesystem guard runs on either endpoint without a checkout.
# Payload must already exist, be empty, and belong to the operation's unique
# .pending-* stage. Every ancestor is opened without following links.
STAGING_CHECK_CODE = r'''
import os,re,stat,sys
from pathlib import Path
path=Path(sys.argv[1]);revision=sys.argv[2]
if not path.is_absolute() or '..' in path.parts or any(ord(c)<32 or ord(c)==127 for c in str(path)):
 raise SystemExit('unsafe transfer staging path')
if path.name!=revision or path.parent.name!='snapshots' or not re.fullmatch(r'\.pending-[0-9a-f]{32}',path.parent.parent.name):
 raise SystemExit('destination is not a snapshot in owned pending staging')
fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
try:
 for part in path.parts[1:]:
  nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
 if os.listdir(fd):raise SystemExit('transfer staging snapshot must be empty')
finally:os.close(fd)
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['map','partition','route','staging-code','stream-command'])
    parser.add_argument('--topology');parser.add_argument('--source-rank',type=int);parser.add_argument('--destination-rank',type=int)
    parser.add_argument('--mode');parser.add_argument('--path');parser.add_argument('--stream',type=int)
    parser.add_argument('--out');parser.add_argument('--remote-ip');parser.add_argument('--netdev');parser.add_argument('--source-ip')
    args=parser.parse_args()
    try:
        if args.command=='map':print(json.dumps(transfer_map(args.topology,args.source_rank,args.destination_rank)))
        elif args.command=='route':validate_ssh_roce_route(json.load(sys.stdin),remote_ip=args.remote_ip,expected_netdev=args.netdev,expected_source_ip=args.source_ip)
        elif args.command=='staging-code':print(STAGING_CHECK_CODE)
        elif args.command=='stream-command':
            import shlex
            print(shlex.join(stream_command(args.mode,args.path,json.load(sys.stdin),args.stream)))
        else:
            groups=partition_files(json.load(sys.stdin));out=Path(args.out)
            for group in groups:
                with (out/f"stream-{group['stream']}.list").open('xb') as handle:
                    handle.write(b''.join(name.encode('ascii')+b'\0' for name in group['files']))
            print(len(groups))
        return 0
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(f'error: transfer: {exc}',file=sys.stderr);return 2



# Pure node-side stream transport. Sender and receiver share exact file names,
# lengths and hashes; only model bytes travel over stdout/stdin, in file order.
# No tar headers or peer-supplied destination paths are trusted.
STREAM_CODE = r'''
import base64,hashlib,json,os,stat,sys
from pathlib import Path
mode,path,encoded=sys.argv[1:]
files=json.loads(base64.b64decode(encoded));root=Path(path)
if mode not in ('send','receive') or not root.is_absolute() or '..' in root.parts:
 raise SystemExit('invalid stream endpoint')
fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
def parent_for(relative):
 parts=relative.split('/')
 if any(p in ('','.','..') for p in parts) or relative.startswith('/') or any(ord(c)<32 or ord(c)==127 for c in relative):
  raise SystemExit('unsafe stream file path')
 parent=os.dup(fd)
 try:
  for part in parts[:-1]:
   if mode=='receive':
    try:os.mkdir(part,mode=0o777,dir_fd=parent)
    except FileExistsError:pass
   child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent);os.close(parent);parent=child
  return parent,parts[-1]
 except BaseException:os.close(parent);raise
try:
 for part in root.parts[1:]:
  child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
 for item in files:
  parent,name=parent_for(item['path']);filefd=None
  try:
   if mode=='send':
    filefd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
    before=os.fstat(filefd)
    if not stat.S_ISREG(before.st_mode) or before.st_size!=item['size']:raise SystemExit('stream source is not expected regular file')
   else:filefd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o666,dir_fd=parent)
   digest=hashlib.sha256();remaining=item['size']
   while remaining:
    chunk=os.read(filefd if mode=='send' else 0,min(4*1024*1024,remaining))
    if not chunk:raise SystemExit('stream ended before expected file length')
    digest.update(chunk);remaining-=len(chunk);out=1 if mode=='send' else filefd
    view=memoryview(chunk)
    while view:
     written=os.write(out,view)
     if written<=0:raise SystemExit('stream write failed')
     view=view[written:]
   if digest.hexdigest()!=item['sha256']:raise SystemExit('stream file hash mismatch')
   if mode=='send':
    after=os.fstat(filefd)
    keys=('st_dev','st_ino','st_mode','st_size','st_mtime_ns','st_ctime_ns')
    if os.read(filefd,1) or any(getattr(before,key)!=getattr(after,key) for key in keys):raise SystemExit('stream source changed during read')
   else:os.fsync(filefd);os.fsync(parent)
  finally:
   if filefd is not None:os.close(filefd)
   os.close(parent)
 if mode=='receive':
  if os.read(0,1):raise SystemExit('stream contains unexpected trailing bytes')
  os.fsync(fd)
finally:os.close(fd)
'''


def stream_command(mode, path, manifest, stream):
    """Build remote argv, with manifest file paths encoded as data, never code."""
    import base64
    manifest=verify_snapshot_manifest(manifest)
    groups=partition_files(manifest)
    if type(stream) is not int or not 0<=stream<len(groups):raise StorageError('invalid stream index')
    by_path={item['path']:item for item in manifest['files']}
    files=[by_path[path] for path in groups[stream]['files']]
    encoded=base64.b64encode(json.dumps(files,separators=(',',':')).encode()).decode()
    if mode not in ('send','receive'):raise StorageError('invalid stream direction')
    return ['python3','-c',STREAM_CODE,mode,path,encoded]

if __name__=='__main__':raise SystemExit(main())
