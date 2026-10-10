import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'python'))
import prefetch_pipeline_wheels as fetch


class Response:
    def __init__(self,start,end,total):
        self.start,self.end=start,end
        self.headers={'Content-Range':f'bytes {start}-{end}/{total}'}
    def read(self,limit):return b'x'*(self.end-self.start+1)
    def close(self):pass
    def __enter__(self):return self
    def __exit__(self,*args):pass


class RangeTests(unittest.TestCase):
    def test_complete_ranges_publish_wheel(self):
        size=fetch.BLOCK+37
        seen=[]
        def open_range(request,timeout):
            value=request.get_header('Range').split('=')[1]
            start,end=map(int,value.split('-'));seen.append((start,end))
            return Response(start,end,size)
        with tempfile.TemporaryDirectory() as d,patch.object(fetch,'urlopen',open_range):
            folder=Path(d);row=fetch.download('https://mirror/example.whl',folder)
            self.assertEqual(row['bytes'],size)
            self.assertEqual((folder/'example.whl').stat().st_size,size)
            self.assertIn((fetch.BLOCK,size-1),seen)
            self.assertFalse((folder/'example.whl.partial').exists())

    def test_ignored_range_never_publishes(self):
        def broken(request,timeout):
            response=Response(0,0,10)
            if request.get_header('Range')!='bytes=0-0':response.headers={}
            return response
        with tempfile.TemporaryDirectory() as d,patch.object(fetch,'urlopen',broken),patch.object(fetch.time,'sleep'):
            folder=Path(d)
            with self.assertRaises(RuntimeError):fetch.download('https://mirror/example.whl',folder)
            self.assertFalse((folder/'example.whl').exists())

if __name__=='__main__':unittest.main()
