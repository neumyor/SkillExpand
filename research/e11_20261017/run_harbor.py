"""Frozen E10 job entrypoint with process-local HTTP/1.1 transport."""
from functools import partial
from pathlib import Path
import runpy

import e2b.sandbox_async.main as sdk
from e2b.api.client_async import get_envd_transport

sdk.get_transport = partial(get_envd_transport, http2=False)
runpy.run_path(str(Path(__file__).parent / 'frozen/runtime/tencent/run_tencent_job.py'), run_name='__main__')
