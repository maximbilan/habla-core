from app.agent.prompts import build_agent_prompt


def test_build_agent_prompt_includes_disclaimer_blocking_rule():
    prompt = build_agent_prompt(
        user_prompt="Ask if the apartment is still available.",
        user_name="Max",
        callee_language_code="es-US",
        callee_language_label="Spanish (US)",
    ).lower()
    assert "never announce translation behavior" in prompt
    assert "processing delays" in prompt

