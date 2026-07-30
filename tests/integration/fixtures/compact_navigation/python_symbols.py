from transformers import GenerationConfig  # ty: ignore[unresolved-import]

ANSWER: int = 42


class Calculator:
    def add(self, left: int, right: int) -> int:
        return left + right


def make_config() -> GenerationConfig:
    return GenerationConfig(max_new_tokens=8)
