"""Tests for the standalone sanitize endpoint (/api/sanitize)"""
import pytest
from unittest.mock import patch


@pytest.mark.anyio
async def test_sanitize_success(client):
    """Successful sanitization returns cleaned data"""
    with patch('app.routers.sanitize.DataSanitizer') as mock_cls:
        mock_cls.sanitize_generic.return_value = {
            'data': [
                {'open': 150.0, 'close': 153.0, 'volume': 1000000.0},
            ],
            'summary': {
                'original_count': 1,
                'cleaned_count': 1,
                'removed_count': 0,
                'removal_percentage': 0.0,
            }
        }

        response = await client.post('/api/sanitize', json={
            'data': [
                {'open': 150.0, 'close': 153.0, 'volume': 1000000.0},
            ],
            'quantile': 0.99,
        })

    assert response.status_code == 200
    data = response.json()
    assert data['success'] is True
    assert len(data['data']) == 1
    assert data['summary']['original_count'] == 1


@pytest.mark.anyio
async def test_sanitize_empty_data(client):
    """Empty data list should still succeed"""
    with patch('app.routers.sanitize.DataSanitizer') as mock_cls:
        mock_cls.sanitize_generic.return_value = {
            'data': [],
            'summary': {'original_count': 0, 'cleaned_count': 0, 'removed_count': 0}
        }

        response = await client.post('/api/sanitize', json={
            'data': [],
        })

    assert response.status_code == 200
    data = response.json()
    assert data['success'] is True
    assert len(data['data']) == 0


@pytest.mark.anyio
async def test_sanitize_custom_quantile(client):
    """Custom quantile should be passed to the sanitizer"""
    with patch('app.routers.sanitize.DataSanitizer') as mock_cls:
        mock_cls.sanitize_generic.return_value = {
            'data': [{'open': 150.0}],
            'summary': {'original_count': 1, 'cleaned_count': 1, 'removed_count': 0}
        }

        await client.post('/api/sanitize', json={
            'data': [{'open': 150.0}],
            'quantile': 0.95,
        })

        mock_cls.sanitize_generic.assert_called_once()
        call_kwargs = mock_cls.sanitize_generic.call_args
        assert call_kwargs.kwargs.get('quantile', call_kwargs.args[1] if len(call_kwargs.args) > 1 else None) == 0.95


@pytest.mark.anyio
async def test_sanitize_error_returns_error_response(client):
    """Sanitizer exception should return error in response body"""
    with patch('app.routers.sanitize.DataSanitizer') as mock_cls:
        mock_cls.sanitize_generic.side_effect = Exception('pandas-dq error')

        response = await client.post('/api/sanitize', json={
            'data': [{'open': 150.0}],
        })

    assert response.status_code == 200  # endpoint catches and returns error in body
    data = response.json()
    assert data['success'] is False
    assert 'pandas-dq error' in data['error']


@pytest.mark.anyio
async def test_sanitize_invalid_quantile_returns_422(client):
    """Quantile outside [0.0, 1.0] should fail validation"""
    response = await client.post('/api/sanitize', json={
        'data': [{'open': 150.0}],
        'quantile': 1.5,
    })

    assert response.status_code == 422
