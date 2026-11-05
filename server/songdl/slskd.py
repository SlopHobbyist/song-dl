"""Minimal async client for the slskd REST API (v0)."""

import asyncio
from urllib.parse import quote

import aiohttp


class SlskdError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Slskd:
    def __init__(self, url, api_key):
        self.base = url + '/api/v0'
        self.api_key = api_key
        self.session = None

    async def start(self):
        self.session = aiohttp.ClientSession(
            headers={'X-API-Key': self.api_key},
            timeout=aiohttp.ClientTimeout(total=30),
        )

    async def close(self):
        if self.session:
            await self.session.close()

    async def request(self, method, path, body=None, params=None):
        try:
            async with self.session.request(method, self.base + path, json=body, params=params) as res:
                text = await res.text()
                if res.status >= 400:
                    # slskd returns plain strings (sometimes JSON-quoted) as error bodies
                    raise SlskdError(text.strip().strip('"') or res.reason, res.status)
                if not text:
                    return None
                return await res.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise SlskdError(f'slskd is unreachable: {e!r}') from e

    # Server

    async def server_state(self):
        return await self.request('GET', '/server')

    # Searches

    async def start_search(self, search_id, text, **options):
        return await self.request('POST', '/searches', {'id': search_id, 'searchText': text, **options})

    async def get_search(self, search_id):
        return await self.request('GET', f'/searches/{search_id}')

    async def stop_search(self, search_id):
        return await self.request('PUT', f'/searches/{search_id}')

    async def search_responses(self, search_id):
        return await self.request('GET', f'/searches/{search_id}/responses')

    async def delete_search(self, search_id):
        return await self.request('DELETE', f'/searches/{search_id}')

    # Downloads

    async def enqueue(self, username, filename, size, destination):
        return await self.request('POST', '/transfers/downloads/batches', {
            'username': username,
            'files': [{'filename': filename, 'size': size}],
            'options': {'destination': destination},
        })

    async def get_download(self, username, transfer_id):
        return await self.request('GET', f'/transfers/downloads/{quote(username, safe="")}/{transfer_id}')

    async def queue_position(self, username, transfer_id):
        return await self.request('GET', f'/transfers/downloads/{quote(username, safe="")}/{transfer_id}/position')

    async def cancel_download(self, username, transfer_id):
        return await self.request('DELETE', f'/transfers/downloads/{quote(username, safe="")}/{transfer_id}',
                                  params={'remove': 'true'})

    async def clear_completed_downloads(self):
        return await self.request('DELETE', '/transfers/downloads/all/completed')

    # Shares

    async def rescan_shares(self):
        return await self.request('PUT', '/shares', {})

    # Private messages

    async def conversations(self, unacknowledged=False):
        return await self.request('GET', '/conversations',
                                  params={'unAcknowledgedOnly': str(unacknowledged).lower()})

    async def messages(self, username, unacknowledged=False):
        return await self.request('GET', f'/conversations/{quote(username, safe="")}/messages',
                                  params={'unAcknowledgedOnly': str(unacknowledged).lower()})

    async def acknowledge(self, username, message_id):
        return await self.request('PUT', f'/conversations/{quote(username, safe="")}/{message_id}')

    async def send_message(self, username, text):
        return await self.request('POST', f'/conversations/{quote(username, safe="")}', text)
