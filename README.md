# Reader Mode Service

A small FastAPI service that accepts a URL, fetches the page, and returns a clean reading-mode HTML document with the main article content and images.

## API

`POST /reader`

```json
{
  "url": "https://example.com/article"
}
```

The response body is `text/html`.

## Run Locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Then:

```bash
curl -X POST http://127.0.0.1:8000/reader \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}'
```

## Notes

- Only `http` and `https` URLs are accepted.
- Local/private network addresses are blocked before fetching.
- Redirects and response size are bounded.
- The extracted HTML is sanitized before being returned.
