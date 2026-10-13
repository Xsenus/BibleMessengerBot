"""The authenticated gallery uses blob image previews under a strict panel policy."""
import httpx
import pytest

from app.web.main import app


@pytest.mark.asyncio
async def test_panel_policy_allows_gallery_blob_images_without_relaxing_scripts():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/admin/overview')
    assert response.status_code == 401
    directives = {part.strip().split()[0]: part.strip().split()[1:]
                  for part in response.headers['Content-Security-Policy'].split(';')}
    assert directives['img-src'] == ["'self'", 'data:', 'blob:']
    assert directives['default-src'] == ["'self'"]
    assert directives['frame-ancestors'] == ["'none'"]
    assert response.headers['Cache-Control'] == 'no-store'
