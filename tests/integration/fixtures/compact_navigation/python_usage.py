from python_symbols import ANSWER, Calculator


def calculate() -> int:
    return Calculator().add(ANSWER, 1)
