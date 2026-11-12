from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..graph.build import build_graph, focus_options
from ..models import User
from ..schemas import GraphOut
from ..security import current_user

router = APIRouter(tags=["graph"])


@router.get("/graph", response_model=GraphOut)
def graph(focus: str = Query(..., examples=["entity:sup-b", "employee:e-petrova", "task:t-certificates-1104"]),
          user: User = Depends(current_user), db: Session = Depends(get_db)) -> GraphOut:
    out = build_graph(db, user, focus)
    audit.write(db, user, "graph.view", object_type="graph", object_id=focus,
                details={"nodes": len(out.nodes), "edges": len(out.edges)})
    return out


@router.get("/graph/focus-options")
def options(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return focus_options(db)
