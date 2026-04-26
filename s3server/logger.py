import datetime
from typing import Optional


def log(msg: str, log_file: Optional[str] = None) -> None:
    """
    Shared logging utility.
    - Prints to console
    - Appends to file when log_file is provided
    """
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {msg}"
    print(line)
    if not log_file:
        return
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        # Keep service running even if file logging fails
        pass
