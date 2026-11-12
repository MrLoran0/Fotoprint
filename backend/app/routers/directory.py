"""Справочники и документы. Права проверяются на сервере; отказ пишется в аудит."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import Document, Employee, User
from ..schemas import EmployeeOut
from ..security import can_read_document, current_user, readable_document_filter

router = APIRouter(tags=["directory"])


class DocumentListItem(BaseModel):
    id: str
    title: str
    version: int
    doc_date: date
    author: str
    access_level: str
    department_id: str | None
    ai_allowed: bool


class DocumentFull(DocumentListItem):
    content: str


@router.get("/employees", response_model=list[EmployeeOut])
def employees(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[Employee]:
    return list(db.scalars(select(Employee).order_by(Employee.full_name)).all())


@router.get("/documents", response_model=list[DocumentListItem])
def documents(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[DocumentListItem]:
    rows = db.scalars(select(Document).where(readable_document_filter(user, for_ai=False)).order_by(Document.title)).all()
    return [DocumentListItem.model_validate(d, from_attributes=True) for d in rows]


@router.get("/documents/{doc_id}", response_model=DocumentFull)
def document(doc_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> DocumentFull:
    doc = db.get(Document, doc_id)
    # одинаковый ответ для «нет такого» и «нет доступа» — не раскрываем существование закрытого документа
    if doc is None or not can_read_document(user, doc):
        audit.write(db, user, "document.open", object_type="document", object_id=doc_id, result="denied")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Документ недоступен")
    audit.write(db, user, "document.open", object_type="document", object_id=doc.id)
    return DocumentFull.model_validate(doc, from_attributes=True)
