# pydantic

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

pydantic is a Python library that checks data against a description. You write a class that lists fields and their types. When you build an object from untrusted data, such as JSON from a request, pydantic checks every field, converts what it can, and raises an error that lists exactly what was wrong.

## Why this repository uses it

Every request body in the chain is a small JSON document: a search query, a stock check, a profile name, a fault setting. pydantic checks each one at the edge of the service, so the use cases receive plain valid values and the HTTP layer answers 422 for the rest.

## Where it appears

| Path | What it does with pydantic |
| --- | --- |
| `services/gateway/src/gateway/interfaces/api.py` | `SearchBody`: `query`, 1 to 200 characters |
| `services/stock/src/stock/interfaces/api.py` | `StockBody` (`sku`), `FaultsBody` (`latency_ms`, `fail_rate`, both optional) |
| `libs/chassis/src/chassis/admin.py` | `ProfileBody` (`name`) |
| `services/search/...`, `services/pricing/...` | Their request bodies (`query` and `sku`) |

pydantic is not listed in any `pyproject.toml` of its own; it is installed as a dependency of FastAPI (`pydantic 2.14.1` in `uv.lock`).

## How it works underneath

- A model is a class that inherits from `BaseModel`. Its annotated attributes are the fields.
- `Model.model_validate(data)` takes a dictionary and returns a model, or raises `ValidationError`. `error.errors()` returns a list; each entry has a `loc` (where) and a `msg` (what).
- **Aliases.** A field can have a different name on the wire than in Python: `customer_id: str = Field(alias="customerId")`. JSON uses the alias; Python code uses the field name. `populate_by_name=True` also accepts the field name.
- `model_dump(by_alias=True)` turns a model back into a dictionary using the aliases.
- The checking code is compiled (written in Rust), so it is fast.

FastAPI (see `fastapi.md`) calls pydantic for you whenever a handler parameter is a model.

## Walkthrough of this repository's use

### A model with limits

```python
class SearchBody(BaseModel):
    """``POST /search``."""

    query: str = Field(min_length=1, max_length=200)

```

`Field(min_length=1, max_length=200)` adds limits to a text field. An empty query or one longer than 200 characters is refused with 422 before the use case runs. There is no alias here: the wire name and the Python name are both `query`.

### Optional fields

```python
class StockBody(BaseModel):
    """``POST /stock``."""

    sku: str


class FaultsBody(BaseModel):
    """``POST /admin/config``: any field left out keeps its value."""

    latency_ms: int | None = None
    fail_rate: float | None = None

```

`latency_ms: int | None = None` means "an integer or nothing, nothing by default". The docstring says what that buys: "any field left out keeps its value". The handler (lines 69 to 81) only overrides what was sent, and then builds the domain object `Faults`, which does its own range check and raises `InvalidFaults` (answered as 422).

### The profile switch

```python
class ProfileBody(BaseModel):
    """``POST /admin/profile``."""

    name: str


def admin_router(holder: ProfileHolder | None, store: SpanStore) -> APIRouter:
    """``/admin/profile`` (when the service has a profile) and ``/admin/traces``.

    These routes change behavior and expose internals. They exist so the experiments can drive the chain;
    the compose file publishes every port on 127.0.0.1 only, and a real deployment would not mount this router.
    """
    router = APIRouter(prefix="/admin", tags=["admin"])

    if holder is not None:

        @router.get("/profile")
        async def read_profile() -> dict[str, str]:
            return {"name": holder.get().value}

        @router.post("/profile")
        async def set_profile(body: ProfileBody) -> dict[str, str]:
            try:
                holder.set(Profile(body.name))
            except ValueError as error:
                raise HTTPException(status_code=422, detail=f"unknown profile {body.name!r}") from error
            return {"name": holder.get().value}
```

`ProfileBody` has one field. `Profile(body.name)` converts the text into the enumeration and raises `ValueError` for an unknown name, which the route turns into a 422 with a clear message.

### Where the tests use these models

The `test_api_and_settings.py` files of the gateway, search, pricing and stock services post JSON bodies and expect 422 for invalid ones (an empty query, an out-of-range fault rate); `libs/chassis/tests/unit/test_tracing_observability_upstream.py` posts a profile name and expects 422 for an unknown one. See `pytest.md`.

## Try it yourself

1. Validate a good and a bad search body:

   ```bash
   uv run python -c "
   from gateway.interfaces.api import SearchBody
   print(SearchBody(query='widget'))
   try:
       SearchBody(query='')
   except Exception as e:
       print(type(e).__name__, [(x['loc'], x['type']) for x in e.errors()])"
   ```

   Expect `query='widget'` and then `ValidationError [(('query',), 'string_too_short')]`.

2. See optional fields:

   ```bash
   uv run python -c "
   from stock.interfaces.api import FaultsBody
   print(FaultsBody(latency_ms=1500))
   print(FaultsBody.model_validate({}).model_dump())"
   ```

   Expect `latency_ms=1500 fail_rate=None` and `{'latency_ms': None, 'fail_rate': None}`.

## Mistakes people make

- **Thinking pydantic only checks types.** It also converts: `"5"` can become `5`. Use strict types where that is not wanted.
- **Mixing the wire model and the domain model.** A request model describes JSON; the domain should not depend on it. Here the models stay in `interfaces/` and the handler builds the domain object.
- **Forgetting that aliases change the JSON names.** Renaming an alias is a breaking change for every caller.
- **Catching `Exception` instead of `ValidationError`.**
- **Using the v1 API** (`parse_obj`, `.dict()`) on a v2 library; the v2 names are `model_validate` and `model_dump`.

## Check yourself

1. What does a pydantic model do with bad input?
   Answer: raises `ValidationError` listing every problem.
2. What is an alias used for?
   Answer: a JSON field name that differs from the Python attribute name.
3. What does `populate_by_name=True` allow?
   Answer: building the model with the Python field name as well as the alias.
4. What does `model_dump(by_alias=True)` return?
   Answer: a dictionary with the alias names.
5. Why keep request models out of the domain layer?
   Answer: so the domain does not depend on the wire format or the web framework.

## Further reading

- pydantic documentation (this repository pins pydantic 2.14): https://docs.pydantic.dev/latest/
- Models: https://docs.pydantic.dev/latest/concepts/models/
- Aliases: https://docs.pydantic.dev/latest/concepts/alias/
