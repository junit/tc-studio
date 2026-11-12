"""Reviewed Python policies. Dependency defaults must not silently widen them."""

from types import MappingProxyType

AGENT_IMPORTS = (
    "collections", "csv", "datetime", "itertools", "json", "math", "queue",
    "random", "re", "stat", "statistics", "time", "unicodedata",
)
# Preserve the locked evaluator's historical baseline for low-level callers
# passing [], but do not inherit new defaults from future dependency versions.
AGENT_BASE_IMPORTS = tuple(name for name in AGENT_IMPORTS if name not in {"csv", "json"})
SKILL_MODULE_ATTRIBUTES = MappingProxyType({
    "csv": frozenset({"reader", "writer", "DictReader", "DictWriter", "QUOTE_ALL", "QUOTE_MINIMAL"}),
    "datetime": frozenset({"datetime", "date", "time", "timedelta", "timezone"}),
    "json": frozenset({"dumps", "loads", "dump", "load", "JSONDecodeError"}),
    "math": frozenset({"ceil", "floor", "sqrt", "pi", "fabs", "isfinite", "isnan"}),
    "random": frozenset({"randint", "randrange", "choice", "choices", "sample", "shuffle", "random", "uniform"}),
    "re": frozenset({"compile", "match", "search", "sub", "findall", "split", "escape", "IGNORECASE"}),
    "time": frozenset({"time", "sleep", "strftime", "localtime", "gmtime"}),
})
SKILL_IMPORTS = frozenset(SKILL_MODULE_ATTRIBUTES)


def create_local_evaluator(imports, **kwargs):
    from smolagents.local_python_executor import LocalPythonExecutor

    # LocalPythonExecutor adds BASE_BUILTIN_MODULES itself. Replace its resolved
    # list as well, so a dependency upgrade cannot add an unreviewed module.
    evaluator = LocalPythonExecutor(list(imports), **kwargs)
    evaluator.authorized_imports = list(dict.fromkeys((*AGENT_BASE_IMPORTS, *imports)))
    return evaluator
