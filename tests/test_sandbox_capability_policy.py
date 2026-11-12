"""Probe the real CodeAgent evaluator without files, networks or credentials."""

from __future__ import annotations

import pytest

from core.agent.constants import AUTHORIZED_IMPORTS
from core.agent.engine import KillablePythonExecutor


@pytest.mark.parametrize(
    "code",
    [
        "import random\nprint(random._os.getpid())",
        "import re\nprint(re.enum.sys.getrecursionlimit())",
        "from random import _os\nprint(_os.getpid())",
        "import random\nprint(getattr(random, '_' + 'os').getpid())",
        "import math\nprint(getattr(math.sqrt, '__module__'))",
    ],
)
def test_main_agent_rejects_indirect_module_and_reflection_access(code):
    executor = KillablePythonExecutor(AUTHORIZED_IMPORTS, force_spawn=True, timeout_seconds=2)
    try:
        executor.send_tools({})
        with pytest.raises(Exception, match="禁止|not allowed|Forbidden|attribute|属性|import"):
            executor(code)
    finally:
        executor.close()


@pytest.mark.parametrize("module", ["os", "sys", "subprocess", "requests", "pandas", "pathlib"])
def test_main_agent_rejects_modules_outside_import_allowlist(module):
    executor = KillablePythonExecutor(AUTHORIZED_IMPORTS, force_spawn=True, timeout_seconds=2)
    try:
        executor.send_tools({})
        with pytest.raises(Exception, match="not allowed|禁止"):
            executor(f"import {module}")
    finally:
        executor.close()


def test_main_agent_keeps_public_calculation_and_registered_capabilities():
    executor = KillablePythonExecutor(AUTHORIZED_IMPORTS, force_spawn=True, timeout_seconds=2)
    executor.additional_functions["render_sample"] = lambda title: {"title": title, "ok": True}
    try:
        executor.send_tools({})
        result = executor(
            "import json, math, random, statistics\n"
            "from collections import Counter\n"
            "from itertools import islice\n"
            "sample = render_sample('report')\n"
            "print(math.sqrt(9), statistics.mean([2, 4]), random.randint(1, 1))\n"
            "print(Counter(['a', 'a'])['a'], list(islice(range(5), 2)))\n"
            "print(getattr(sample, 'get')('ok'))\n"
            "print(json.loads(json.dumps(sample))['title'])"
        )
        assert result.logs.strip().splitlines() == ["3.0 3 1", "2 [0, 1]", "True", "report"]
    finally:
        executor.close()
