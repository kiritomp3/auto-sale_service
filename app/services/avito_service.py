from __future__ import annotations

import asyncio
import csv
import json
import logging
import secrets
import string
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx

from app.clients.avito_client import AvitoClient
from app.models import (
    AvitoAccount,
    AvitoAccountResponse,
    AvitoAuthLoginResponse,
    AvitoExportFormat,
    AvitoListingDraft,
    AvitoQueuedListing,
    AvitoQueueResponse,
    AvitoScheduleResponse,
    AvitoSession,
    AvitoValidationIssue,
    AvitoValidationResponse,
)
from app.repositories.avito_repository import RedisAvitoRepository


ACTIVE_STATUSES = {"queued", "scheduled"}
REORDERABLE_STATUSES = {"queued", "scheduled", "failed"}
logger = logging.getLogger(__name__)


class AvitoService:
    def __init__(
        self,
        repository: RedisAvitoRepository,
        base_url: str,
        publish_path: str,
        session_ttl_seconds: int,
        publish_interval_seconds: int,
        scheduler_interval_seconds: int,
        output_dir: Path,
    ):
        if publish_interval_seconds < 3700:
            raise ValueError("Avito publish interval cannot be lower than 3700 seconds")
        self._repository = repository
        self._base_url = base_url
        self._publish_path = publish_path
        self._session_ttl_seconds = session_ttl_seconds
        self._publish_interval_seconds = publish_interval_seconds
        self._scheduler_interval_seconds = scheduler_interval_seconds
        self._output_dir = output_dir
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def publish_interval_seconds(self) -> int:
        return self._publish_interval_seconds

    @property
    def session_ttl_seconds(self) -> int:
        return self._session_ttl_seconds

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def login(
        self,
        avito_account_id: str,
        avito_access_token: str,
        avito_refresh_token: str = "",
        account_name: str = "",
    ) -> AvitoAuthLoginResponse:
        now = self._now()
        existing = self._repository.get_account(avito_account_id)
        account = AvitoAccount(
            account_id=avito_account_id,
            name=account_name or (existing.name if existing else ""),
            avito_access_token=avito_access_token,
            avito_refresh_token=avito_refresh_token,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            last_api_attempt_at=existing.last_api_attempt_at if existing else None,
            last_published_at=existing.last_published_at if existing else None,
        )
        self._repository.save_account(account)

        token = secrets.token_urlsafe(48)
        session = AvitoSession(
            token=token,
            account_id=avito_account_id,
            expires_at=self._repository.build_expiry(self._session_ttl_seconds),
        )
        self._repository.save_session(session, ttl_seconds=self._session_ttl_seconds)
        return AvitoAuthLoginResponse(
            access_token=token,
            expires_in=self._session_ttl_seconds,
            expires_at=session.expires_at,
            account_id=avito_account_id,
        )

    def logout(self, token: str) -> None:
        self._repository.delete_session(token)

    def get_session(self, token: str) -> AvitoSession | None:
        return self._repository.get_valid_session(token)

    def get_account(self, account_id: str) -> AvitoAccount | None:
        return self._repository.get_account(account_id)

    def get_account_response(self, account_id: str) -> AvitoAccountResponse:
        account = self._require_account(account_id)
        return AvitoAccountResponse(
            account_id=account.account_id,
            name=account.name,
            created_at=account.created_at,
            updated_at=account.updated_at,
            last_api_attempt_at=account.last_api_attempt_at,
            last_published_at=account.last_published_at,
        )

    def validate_draft(self, draft: AvitoListingDraft) -> AvitoValidationResponse:
        normalized = self._normalize_draft(draft)
        issues = self._validate_payload(normalized)
        errors = [issue for issue in issues if issue.severity == "error"]
        warnings = [issue for issue in issues if issue.severity == "warning"]
        return AvitoValidationResponse(
            ok=not errors,
            errors=errors,
            warnings=warnings,
            normalized_item=normalized,
        )

    def queue_items(self, account_id: str, drafts: list[AvitoListingDraft]) -> AvitoQueueResponse:
        self._require_account(account_id)
        now = self._now()
        listings: list[AvitoQueuedListing] = []
        validation_errors: list[str] = []

        for index, draft in enumerate(drafts, start=1):
            normalized = self._normalize_draft(draft)
            issues = self._validate_payload(normalized)
            errors = [issue for issue in issues if issue.severity == "error"]
            if errors:
                details = "; ".join(f"{issue.field}: {issue.message}" for issue in errors)
                validation_errors.append(f"item {index}: {details}")
                continue

            listing_id = uuid.uuid4().hex
            normalized["external_id"] = normalized.get("external_id") or listing_id
            listing = AvitoQueuedListing(
                listing_id=listing_id,
                account_id=account_id,
                status="queued",
                payload=normalized,
                created_at=now,
                updated_at=now,
            )
            listings.append(listing)

        if validation_errors:
            raise ValueError("Cannot queue invalid Avito listings: " + " | ".join(validation_errors))

        for listing in listings:
            self._repository.append_listing(listing)

        scheduled = self.recalculate_schedule(account_id)
        return AvitoQueueResponse(
            account_id=account_id,
            publish_interval_seconds=self._publish_interval_seconds,
            total=len(scheduled),
            items=scheduled,
        )

    def get_schedule(self, account_id: str) -> AvitoScheduleResponse:
        self._require_account(account_id)
        items = self.recalculate_schedule(account_id)
        return AvitoScheduleResponse(
            account_id=account_id,
            publish_interval_seconds=self._publish_interval_seconds,
            items=items,
        )

    def get_listing(self, account_id: str, listing_id: str) -> AvitoQueuedListing:
        listing = self._repository.get_listing(account_id, listing_id)
        if listing is None:
            raise KeyError(listing_id)
        return listing

    def cancel_listing(self, account_id: str, listing_id: str) -> AvitoQueuedListing:
        listing = self.get_listing(account_id, listing_id)
        if listing.status == "published":
            raise ValueError("Already published Avito listings cannot be cancelled")
        if listing.status == "publishing":
            raise ValueError("Avito listing is being published now and cannot be cancelled")
        now = self._now()
        listing.status = "cancelled"
        listing.cancelled_at = now
        listing.estimated_publish_at = None
        listing.updated_at = now
        self._repository.save_listing(listing)
        self.recalculate_schedule(account_id)
        return self.get_listing(account_id, listing_id)

    def retry_listing(self, account_id: str, listing_id: str) -> AvitoQueuedListing:
        listing = self.get_listing(account_id, listing_id)
        if listing.status != "failed":
            raise ValueError("Only failed Avito listings can be restarted")
        now = self._now()
        listing.status = "queued"
        listing.error = None
        listing.estimated_publish_at = None
        listing.updated_at = now
        self._repository.save_listing(listing)
        self.recalculate_schedule(account_id)
        return self.get_listing(account_id, listing_id)

    def reorder(self, account_id: str, listing_ids: list[str]) -> AvitoScheduleResponse:
        existing_ids = self._repository.list_listing_ids(account_id)
        existing_set = set(existing_ids)
        missing = [listing_id for listing_id in listing_ids if listing_id not in existing_set]
        if missing:
            raise KeyError(", ".join(missing))

        blocked: list[str] = []
        for listing_id in listing_ids:
            listing = self.get_listing(account_id, listing_id)
            if listing.status not in REORDERABLE_STATUSES:
                blocked.append(listing_id)
        if blocked:
            raise ValueError("Only queued, scheduled or failed Avito listings can be reordered")

        new_order = list(dict.fromkeys(listing_ids))
        new_order.extend(listing_id for listing_id in existing_ids if listing_id not in new_order)
        self._repository.replace_listing_order(account_id, new_order)
        return self.get_schedule(account_id)

    def recalculate_schedule(self, account_id: str) -> list[AvitoQueuedListing]:
        account = self._require_account(account_id)
        now = self._now()
        next_at = now
        if account.last_api_attempt_at is not None:
            next_at = max(next_at, account.last_api_attempt_at + timedelta(seconds=self._publish_interval_seconds))

        listings = self._repository.list_listings(account_id)
        for position, listing in enumerate(listings, start=1):
            listing.position = position
            if listing.status in ACTIVE_STATUSES:
                listing.status = "scheduled"
                listing.estimated_publish_at = next_at
                next_at = next_at + timedelta(seconds=self._publish_interval_seconds)
                listing.updated_at = now
                self._repository.save_listing(listing)
            elif listing.status != "publishing" and listing.estimated_publish_at is not None:
                listing.estimated_publish_at = None
                listing.updated_at = now
                self._repository.save_listing(listing)
        return self._repository.list_listings(account_id)

    async def run_scheduler_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.process_due_publications)
            except Exception:
                logger.exception("Avito scheduler iteration failed")
            await asyncio.sleep(self._scheduler_interval_seconds)

    def process_due_publications(self) -> int:
        published_or_attempted = 0
        for account_id in self._repository.list_account_ids():
            account = self._repository.get_account(account_id)
            if account is None:
                continue
            self.recalculate_schedule(account_id)
            account = self._repository.get_account(account_id)
            if account is None:
                continue

            now = self._now()
            if account.last_api_attempt_at is not None:
                next_allowed = account.last_api_attempt_at + timedelta(seconds=self._publish_interval_seconds)
                if next_allowed > now:
                    continue

            listing = self._next_due_listing(account.account_id, now)
            if listing is None:
                continue
            self._publish_one(account, listing)
            published_or_attempted += 1
        return published_or_attempted

    def export_listings(self, account_id: str, export_format: AvitoExportFormat) -> tuple[Path, str, str]:
        self._require_account(account_id)
        listings = self._repository.list_listings(account_id)
        rows = self._export_rows(listings)
        safe_account_id = self._safe_filename_part(account_id)
        timestamp = self._now().strftime("%Y%m%d_%H%M%S")
        filename = f"avito_{safe_account_id}_{timestamp}.{export_format}"
        path = self._output_dir / filename

        if export_format == "csv":
            self._write_csv(path, rows)
            return path, "text/csv; charset=utf-8", filename
        if export_format == "xlsx":
            self._write_xlsx(path, rows)
            return path, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename
        if export_format == "xml":
            self._write_xml(path, listings)
            return path, "application/xml", filename
        raise ValueError(f"Unsupported Avito export format: {export_format}")

    def _require_account(self, account_id: str) -> AvitoAccount:
        account = self._repository.get_account(account_id)
        if account is None:
            raise KeyError(account_id)
        return account

    def _normalize_draft(self, draft: AvitoListingDraft) -> dict[str, Any]:
        listing_content = draft.listing_content or {}
        bullet_points = listing_content.get("bullet_points") or []
        if not isinstance(bullet_points, list):
            bullet_points = [str(bullet_points)]

        title = draft.title or str(listing_content.get("title") or "")
        description = draft.description or str(listing_content.get("full_description") or "")
        if not description and bullet_points:
            description = "\n".join(str(point) for point in bullet_points)

        attributes = dict(draft.attributes)
        for field in ("specifications", "seo_keywords", "search_queries"):
            value = listing_content.get(field)
            if value:
                attributes.setdefault(field, value)
        if listing_content.get("condition"):
            attributes.setdefault("condition", listing_content.get("condition"))
        for field in ("price_hint", "location_hint"):
            value = listing_content.get(field)
            if value:
                attributes.setdefault(field, value)

        normalized = dict(draft.raw_payload)
        normalized.update(
            {
                "external_id": draft.external_id or normalized.get("external_id"),
                "title": title.strip(),
                "description": description.strip(),
                "price": (
                    draft.price
                    if draft.price is not None
                    else listing_content.get("price", normalized.get("price"))
                ),
                "category": (draft.category or str(listing_content.get("category") or "")).strip(),
                "location": (draft.location or str(listing_content.get("location_hint") or "")).strip(),
                "images": draft.images or normalized.get("images") or [],
                "attributes": attributes,
                "contact_name": draft.contact_name or normalized.get("contact_name", ""),
                "phone": draft.phone or normalized.get("phone", ""),
            }
        )
        return normalized

    def _validate_payload(self, payload: dict[str, Any]) -> list[AvitoValidationIssue]:
        issues: list[AvitoValidationIssue] = []

        def add(severity: str, field: str, message: str) -> None:
            issues.append(AvitoValidationIssue(severity=severity, field=field, message=message))

        title = str(payload.get("title") or "").strip()
        description = str(payload.get("description") or "").strip()
        category = str(payload.get("category") or "").strip()
        location = str(payload.get("location") or "").strip()
        images = payload.get("images") or []
        price = payload.get("price")

        if not title:
            add("error", "title", "Укажите заголовок объявления")
        elif len(title) > 50:
            add("warning", "title", "Для Авито лучше держать заголовок до 50 символов")

        if not description:
            add("error", "description", "Добавьте описание товара")
        elif len(description) < 20:
            add("warning", "description", "Описание выглядит слишком коротким")

        if not category:
            add("error", "category", "Укажите категорию Авито")
        if not location:
            add("error", "location", "Укажите город или адрес размещения")

        try:
            numeric_price = float(price)
        except (TypeError, ValueError):
            add("error", "price", "Укажите цену числом")
        else:
            if numeric_price <= 0:
                add("error", "price", "Цена должна быть больше 0")

        if not isinstance(images, list) or not images:
            add("error", "images", "Добавьте хотя бы одно изображение товара")

        if not str(payload.get("contact_name") or "").strip():
            add("warning", "contact_name", "Контакт можно оставить в аккаунте Авито, но в объявлении он не указан")
        if not str(payload.get("phone") or "").strip():
            add("warning", "phone", "Телефон можно оставить в аккаунте Авито, но в объявлении он не указан")

        return issues

    def _next_due_listing(self, account_id: str, now: datetime) -> AvitoQueuedListing | None:
        for listing in self._repository.list_listings(account_id):
            if listing.status != "scheduled":
                continue
            if listing.estimated_publish_at is None or listing.estimated_publish_at <= now:
                return listing
            return None
        return None

    def _publish_one(self, account: AvitoAccount, listing: AvitoQueuedListing) -> None:
        now = self._now()
        listing.status = "publishing"
        listing.updated_at = now
        self._repository.save_listing(listing)

        success = False
        try:
            client = AvitoClient(
                access_token=account.avito_access_token,
                base_url=self._base_url,
                publish_path=self._publish_path,
            )
            response = client.publish_listing(self._build_publish_payload(listing))
            success = True
            listing.status = "published"
            listing.published_at = self._now()
            listing.avito_response = response
            listing.error = None
        except Exception as exc:
            listing.status = "failed"
            listing.error = self._format_publish_error(exc)
        finally:
            finished_at = self._now()
            listing.attempts += 1
            listing.updated_at = finished_at
            listing.estimated_publish_at = None

            account.last_api_attempt_at = finished_at
            if success:
                account.last_published_at = finished_at
            account.updated_at = finished_at

            self._repository.save_account(account)
            self._repository.save_listing(listing)
            self.recalculate_schedule(account.account_id)

    @staticmethod
    def _build_publish_payload(listing: AvitoQueuedListing) -> dict[str, Any]:
        payload = dict(listing.payload)
        payload["external_id"] = payload.get("external_id") or listing.listing_id
        payload["source"] = "auto-sale_service"
        return payload

    @staticmethod
    def _format_publish_error(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                details: Any = exc.response.json()
            except Exception:
                details = exc.response.text
            return f"Avito API error ({exc.response.status_code}): {details}"
        if isinstance(exc, httpx.HTTPError):
            return f"Avito API network error: {exc}"
        return str(exc)

    @staticmethod
    def _export_rows(listings: list[AvitoQueuedListing]) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for listing in listings:
            payload = listing.payload
            rows.append(
                {
                    "listing_id": listing.listing_id,
                    "status": listing.status,
                    "position": str(listing.position),
                    "estimated_publish_at": listing.estimated_publish_at.isoformat() if listing.estimated_publish_at else "",
                    "published_at": listing.published_at.isoformat() if listing.published_at else "",
                    "external_id": str(payload.get("external_id") or ""),
                    "title": str(payload.get("title") or ""),
                    "description": str(payload.get("description") or ""),
                    "price": str(payload.get("price") or ""),
                    "category": str(payload.get("category") or ""),
                    "location": str(payload.get("location") or ""),
                    "images": json.dumps(payload.get("images") or [], ensure_ascii=False),
                    "attributes": json.dumps(payload.get("attributes") or {}, ensure_ascii=False),
                    "error": listing.error or "",
                }
            )
        return rows

    @staticmethod
    def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
        headers = list(rows[0].keys()) if rows else [
            "listing_id",
            "status",
            "position",
            "estimated_publish_at",
            "published_at",
            "external_id",
            "title",
            "description",
            "price",
            "category",
            "location",
            "images",
            "attributes",
            "error",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _write_xlsx(path: Path, rows: list[dict[str, str]]) -> None:
        headers = list(rows[0].keys()) if rows else [
            "listing_id",
            "status",
            "position",
            "estimated_publish_at",
            "published_at",
            "external_id",
            "title",
            "description",
            "price",
            "category",
            "location",
            "images",
            "attributes",
            "error",
        ]
        sheet_rows = [headers] + [[row.get(header, "") for header in headers] for row in rows]
        worksheet = AvitoService._build_xlsx_worksheet(sheet_rows)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
            )
            archive.writestr(
                "_rels/.rels",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
            )
            archive.writestr(
                "xl/workbook.xml",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Avito" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
            )
            archive.writestr("xl/worksheets/sheet1.xml", worksheet)

    @staticmethod
    def _build_xlsx_worksheet(rows: list[list[str]]) -> str:
        xml_rows: list[str] = []
        for row_index, row in enumerate(rows, start=1):
            cells: list[str] = []
            for column_index, value in enumerate(row, start=1):
                ref = f"{AvitoService._xlsx_column(column_index)}{row_index}"
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
            xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(xml_rows)}</sheetData>'
            "</worksheet>"
        )

    @staticmethod
    def _xlsx_column(index: int) -> str:
        result = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            result = f"{string.ascii_uppercase[remainder]}{result}"
        return result

    @staticmethod
    def _write_xml(path: Path, listings: list[AvitoQueuedListing]) -> None:
        root = ET.Element("Ads", {"formatVersion": "3", "target": "Avito.ru"})
        for listing in listings:
            payload = listing.payload
            ad = ET.SubElement(root, "Ad")
            ET.SubElement(ad, "Id").text = str(payload.get("external_id") or listing.listing_id)
            ET.SubElement(ad, "Category").text = str(payload.get("category") or "")
            ET.SubElement(ad, "Title").text = str(payload.get("title") or "")
            ET.SubElement(ad, "Description").text = str(payload.get("description") or "")
            ET.SubElement(ad, "Price").text = str(payload.get("price") or "")
            ET.SubElement(ad, "Address").text = str(payload.get("location") or "")
            images_el = ET.SubElement(ad, "Images")
            for image_url in payload.get("images") or []:
                ET.SubElement(images_el, "Image", {"url": str(image_url)})
            for name, value in (payload.get("attributes") or {}).items():
                attr = ET.SubElement(ad, "Attribute", {"name": str(name)})
                attr.text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)

        ET.indent(root, space="  ")
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def _safe_filename_part(value: str) -> str:
        return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value) or "account"
