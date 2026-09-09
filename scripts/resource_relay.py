#!/usr/bin/env python3
"""Serialize per-rank JSON lines without interleaving concurrent SSH streams."""
import argparse
import fcntl
import json
import os
import stat
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock',required=True)
    parser.add_argument('--rank',default='all')
    parser.add_argument('--error')
    args=parser.parse_args()
    fd=os.open(args.lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('invalid relay lock')
        def emit(value):
            fcntl.flock(fd,fcntl.LOCK_EX)
            try: print(json.dumps(value,sort_keys=True,separators=(',',':')),flush=True)
            finally: fcntl.flock(fd,fcntl.LOCK_UN)
        if args.error:
            emit({'schema_version':1,'kind':'pulsar-resource-error','rank':args.rank,'error':args.error})
            return 0
        for line in sys.stdin:
            try:
                if len(line)>65536: raise ValueError('sample too large')
                sample=json.loads(line)
                if (not isinstance(sample,dict) or type(sample.get('schema_version')) is not int
                        or sample['schema_version']!=1 or sample.get('kind')!='pulsar-model-serving-resource-sample'
                        or sample.get('rank')!=args.rank): raise ValueError('invalid sample')
            except (ValueError,TypeError):
                emit({'schema_version':1,'kind':'pulsar-resource-error','rank':args.rank,'error':'invalid_sample'})
                return 2
            emit(sample)
        return 0
    finally:
        os.close(fd)


if __name__=='__main__': raise SystemExit(main())
