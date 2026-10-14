"""Use process-local HTTP/1.1 for E10 sandbox streams."""
from functools import partial
import runpy

import e2b.sandbox_async.main as sdk
from e2b.api.client_async import get_envd_transport

sdk.get_transport = partial(get_envd_transport, http2=False)
runpy.run_path('/data2/liyishan/tb21-tencent-skill/python/run_tencent_job.py', run_name='__main__')
