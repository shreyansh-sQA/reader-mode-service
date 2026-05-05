from fastapi.testclient import TestClient

from app.main import app, build_reader_html


def test_build_reader_html_keeps_article_and_images() -> None:
    html = """
    <html>
      <head><title>Example Story</title><script>alert("bad")</script></head>
      <body>
        <nav>Navigation</nav>
        <article>
          <h1>Example Story</h1>
          <p>This is the useful article text with enough words to look like content.</p>
          <figure><img src="/image.jpg" alt="Useful image"></figure>
          <aside>Related links</aside>
        </article>
      </body>
    </html>
    """

    result = build_reader_html(html, "https://example.com/posts/story")

    assert "This is the useful article text" in result
    assert "https://example.com/image.jpg" in result
    assert "Useful image" in result
    assert "Navigation" not in result
    assert "Related links" not in result
    assert "<script" not in result


def test_reader_endpoint_returns_html(monkeypatch) -> None:
    async def fake_fetch_page(url: str):
        from app.main import FetchedPage

        return FetchedPage(
            url=url,
            html="""
            <html>
              <head><title>Clean Me</title></head>
              <body><main><h1>Clean Me</h1><p>Main article body.</p></main></body>
            </html>
            """,
        )

    monkeypatch.setattr("app.main.fetch_page", fake_fetch_page)
    client = TestClient(app)

    response = client.post("/reader", json={"url": "https://example.com/story"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Main article body." in response.text


def test_reader_endpoint_rejects_invalid_url() -> None:
    client = TestClient(app)

    response = client.post("/reader", json={"url": "not-a-url"})

    assert response.status_code == 422


def test_reader_get_without_url_returns_form() -> None:
    client = TestClient(app)

    response = client.get("/reader")

    assert response.status_code == 200
    assert "Reader Mode" in response.text
    assert 'name="url"' in response.text
