"""Intercept the native syscall boundary; never open a real kernel device."""
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import diagnostic_kmsg as native


class NativeReads(unittest.TestCase):
    def test_one_readonly_nonblocking_descriptor_and_one_tail(self):
        with patch.object(native.os,'open',return_value=987) as opened, patch.object(native.os,'lseek') as seek:
            self.assertEqual(native.open_kmsg(),987)
        opened.assert_called_once_with('/dev/kmsg',os.O_RDONLY|os.O_NONBLOCK|os.O_CLOEXEC)
        seek.assert_called_once_with(987,0,os.SEEK_END)

    def test_failed_tail_closes_descriptor(self):
        with patch.object(native.os,'open',return_value=987), patch.object(native.os,'lseek',side_effect=OSError('tail failed')), patch.object(native.os,'close') as closed:
            with self.assertRaises(OSError):native.open_kmsg()
        closed.assert_called_once_with(987)

    def test_whole_record_one_read(self):
        raw=b'6,41,42,-;benign\n KEY=value\n'
        with patch.object(native.os,'read',return_value=raw) as read:
            kind,record=native.read_one(987)
        read.assert_called_once_with(987,8192)
        self.assertEqual(kind,'record')
        self.assertEqual(record['raw'],raw.decode())
        self.assertEqual(record['sequence'],41)

    def test_eagain_epipe_eof_and_unexpected_error_stay_distinct(self):
        for error,status in ((errno.EAGAIN,'eagain'),(errno.EPIPE,'epipe')):
            with self.subTest(error=error), patch.object(native.os,'read',side_effect=OSError(error,'controlled')):
                self.assertEqual(native.read_one(987),(status,None))
        with patch.object(native.os,'read',return_value=b''):
            self.assertEqual(native.read_one(987),('eof',None))
        with patch.object(native.os,'read',side_effect=OSError(errno.EIO,'controlled')):
            with self.assertRaises(OSError):native.read_one(987)

    def test_malformed_fragment_or_combined_reads_retain_exact_bytes(self):
        for raw in (b'bad\n',b'6,1,2,-;unterminated',b'6,1,2,-;one\n6,2,3,-;two\n',b'x'*8192):
            with self.subTest(bytes=len(raw)), patch.object(native.os,'read',return_value=raw):
                with self.assertRaises(native.KernelRecordError) as error:native.read_one(987)
                self.assertEqual(error.exception.raw,raw)

    def test_meminfo_missing_is_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'meminfo';path.write_text('MemAvailable: 10485760 kB\nSwapTotal: 0 kB\n')
            with patch.object(native,'MEMINFO_PATH',str(path)):
                with self.assertRaises(KeyError):native.read_meminfo()

    def test_known_zero_swap_is_distinct_from_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'meminfo';path.write_text('MemAvailable: 10485760 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n')
            with patch.object(native,'MEMINFO_PATH',str(path)):
                self.assertEqual(native.read_meminfo()['swap_used_bytes'],0)


if __name__=='__main__':unittest.main()
