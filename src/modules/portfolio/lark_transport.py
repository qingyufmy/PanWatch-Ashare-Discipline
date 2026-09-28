"""Single-attempt async Lark transport; do not retry ambiguous delivery."""
import httpx


class LarkNotifier:
    def add_channel(self, kind, config):
        if kind != 'lark' or not config.get('webhook_token'):
            raise ValueError('LARK_CHANNEL_INVALID')
        self.token = config['webhook_token']

    async def notify_with_result(self, title, content):
        url = 'https://open.larksuite.com/open-apis/bot/v2/hook/' + self.token
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5)) as client:
            reply = await client.post(url, json={
                'msg_type': 'text', 'content': {'text': title + '\n' + content}})
        # A provider response is distinct from a transport exception, which propagates.
        if reply.status_code != 200:
            return {'success': False, 'error': 'LARK_HTTP_' + str(reply.status_code)}
        try:
            body = reply.json()
        except ValueError:
            raise RuntimeError('LARK_RESPONSE_UNREADABLE')
        code = body.get('code', body.get('StatusCode'))
        return {'success': code == 0,
                'error': None if code == 0 else 'LARK_CODE_' + str(code)}
