from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Generic, Literal, TypeVar

from fastapi import Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.core.deps import check_token
from app.services.im.outbound import UploadAttachment


_IMAGE_KEY = Annotated[str, StringConstraints(pattern=r"^img_[A-Za-z0-9_-]+$")]
_FILE_KEY = Annotated[str, StringConstraints(pattern=r"^file_[A-Za-z0-9_-]+$")]
_UPLOAD_LIMITS = {"image": 10 * 1024**2, "file": 30 * 1024**2}
_MAX_UPLOAD_BODY = _UPLOAD_LIMITS["file"] + 128 * 1024


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarkdownMessage(MessageBody):
    type: Literal["markdown"]
    text: str = Field(min_length=1)


class ImageMessage(MessageBody):
    type: Literal["image"]
    image_key: _IMAGE_KEY


class FileMessage(MessageBody):
    type: Literal["file", "audio"]
    file_key: _FILE_KEY


class VideoMessage(MessageBody):
    type: Literal["video"]
    file_key: _FILE_KEY
    image_key: _IMAGE_KEY | None = None


class CardMessage(MessageBody):
    type: Literal["card"]
    content: dict[str, Any] = Field(min_length=1)


Message = Annotated[
    MarkdownMessage | ImageMessage | FileMessage | VideoMessage | CardMessage,
    Field(discriminator="type"),
]


class OutboundBody(BaseModel):
    task_id: int | None = Field(default=None, gt=0)
    idempotency_key: str | None = None
    text: str = ""
    mention_open_ids: list[str] = Field(default_factory=list)
    message: Message | None = None

    @model_validator(mode="after")
    def validate_message(self) -> OutboundBody:
        if self.message is not None:
            if "text" in self.model_fields_set:
                raise ValueError("send either text or message")
            if self.mention_open_ids and self.message.type != "markdown":
                raise ValueError("mention_open_ids is supported for text and Markdown")
        return self


class ReplyBody(OutboundBody):
    message_id: str = Field(min_length=1)
    reply_in_thread: bool = False
    bot_id: str | None = None


class RespondBody(OutboundBody):
    inbound_id: int


class SendBody(OutboundBody):
    chat_id: str = Field(min_length=1)
    bot_id: str | None = None


Body = TypeVar("Body", bound=OutboundBody)


@dataclass
class OutboundRequest(Generic[Body]):
    body: Body
    upload: UploadAttachment | None = None

    def content_kwargs(self) -> dict[str, Any]:
        args: dict[str, Any] = {
            "text": self.body.text,
            "mention_open_ids": self.body.mention_open_ids,
        }
        if self.body.message is not None:
            args["message"] = self.body.message.model_dump(exclude_none=True)
        if self.upload is not None:
            args["upload"] = self.upload
        return args


class RequestTooLarge(MultiPartException):
    pass


async def _bounded_upload_stream(request: Request) -> AsyncGenerator[bytes, None]:
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_UPLOAD_BODY:
            raise RequestTooLarge("multipart request exceeds the upload size limit")
        yield chunk


def _validate_body(model: type[Body], raw: bytes | str) -> Body:
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_input=False, include_context=False, include_url=False),
        ) from exc


def read_outbound(model: type[Body]) -> Callable[..., AsyncIterator[OutboundRequest[Body]]]:
    async def parse(
        request: Request, authorization: str | None = Header(default=None)
    ) -> AsyncIterator[OutboundRequest[Body]]:
        check_token(authorization)
        media = request.headers.get("content-type", "application/json").partition(";")[0].lower()
        if media.strip() == "application/json" or (
            media.startswith("application/") and media.strip().endswith("+json")
        ):
            yield OutboundRequest(_validate_body(model, await request.body()))
            return
        if media.strip() != "multipart/form-data":
            raise HTTPException(status_code=415, detail="expected JSON or multipart/form-data")

        try:
            form = await MultiPartParser(
                request.headers, _bounded_upload_stream(request), max_files=1, max_fields=2
            ).parse()
        except RequestTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except (MultiPartException, MultipartParseError) as exc:
            raise HTTPException(status_code=422, detail="invalid multipart request") from exc

        try:
            if set(form) != {"payload", "type", "file"} or len(form.multi_items()) != 3:
                raise HTTPException(status_code=422, detail="expected payload, type and one file")
            payload, kind, file = form["payload"], form["type"], form["file"]
            if not isinstance(payload, str) or not isinstance(kind, str) or kind not in _UPLOAD_LIMITS:
                raise HTTPException(status_code=422, detail="expected JSON payload and type image or file")
            if not isinstance(file, UploadFile) or not file.filename or not file.size:
                raise HTTPException(status_code=422, detail="a nonempty file upload is required")
            if file.size > _UPLOAD_LIMITS[kind]:
                raise HTTPException(status_code=413, detail=f"{kind} exceeds the upload size limit")

            body = _validate_body(model, payload)
            if body.model_fields_set & {"text", "message"} or body.mention_open_ids:
                raise HTTPException(
                    status_code=422, detail="upload payload cannot contain text, message or mentions"
                )
            yield OutboundRequest(body, UploadAttachment(kind, file.filename, file.file))
        finally:
            await form.close()

    return parse


def outbound_openapi(operation: str, model: type[OutboundBody]) -> dict[str, Any]:
    # The JSON schema is embedded in the operation because the same dependency
    # reads both encodings. Its variant schemas live alongside it in $defs.
    variant_ref = (
        f"#/paths/~1im~1{operation}/post/requestBody/content/application~1json/schema/$defs/"
        "{model}"
    )
    json_schema = model.model_json_schema(ref_template=variant_ref)
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": json_schema},
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["payload", "type", "file"],
                        "properties": {
                            "payload": {
                                "type": "string",
                                "description": (
                                    f"JSON target and options from {model.__name__}. "
                                    "Omit text, message and mention_open_ids."
                                ),
                            },
                            "type": {"type": "string", "enum": ["image", "file"]},
                            "file": {
                                "type": "string",
                                "format": "binary",
                                "description": "One nonempty image (10 MiB) or file (30 MiB).",
                            },
                        },
                    },
                },
            },
        },
        "responses": {
            "413": {"description": "Upload size exceeded"},
            "415": {"description": "Unsupported request content type"},
            "422": {"description": "Invalid message or upload"},
            "502": {"description": "Feishu CLI failed"},
        },
    }
