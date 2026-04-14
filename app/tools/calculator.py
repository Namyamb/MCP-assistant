import math
def evaluate_expression(expr):
    # Safe evaluator or simply eval for proxy
    try:
        return eval(expr, {"__builtins__": None}, {"math": math})
    except Exception as e:
        return f"Error: {e}"
