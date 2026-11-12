"""Служебные команды:  python -m app.cli seed | reset

docker compose exec api python -m app.cli reset   — удалить все данные стенда и загрузить seed заново
"""

import logging
import sys

from .db import SessionLocal, drop_all, init_db
from .seed import seed_all


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cmd = argv[1] if len(argv) > 1 else "help"
    if cmd == "seed":
        init_db()
        with SessionLocal() as db:
            print(seed_all(db))
        return 0
    if cmd == "reset":
        drop_all()
        init_db()
        with SessionLocal() as db:
            print("reset done:", seed_all(db))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
