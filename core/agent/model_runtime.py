"""Startup validation for the mandatory OpenAI-compatible model adapter."""


def check_model_runtime() -> None:
    """Construct/close the real adapter without reading credentials or doing I/O."""
    try:
        from smolagents import OpenAIServerModel

        model = OpenAIServerModel(
            model_id="tc-studio-runtime-check",
            api_base="https://model-runtime.invalid/v1",
            api_key="synthetic-runtime-check",
        )
    except ImportError as exc:
        raise RuntimeError(
            "TC Studio 模型运行依赖不完整：需要 smolagents[openai]。"
            "请在项目目录执行 uv sync --locked，或运行 scripts/install_locked.sh，"
            "并使用项目 .venv/bin/python 启动服务。"
        ) from exc
    model.client.close()
