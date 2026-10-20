"""All upstream replies in this suite are MOCK FIXTURES, never live API calls."""
import importlib.util
import unittest


class BootstrapTests(unittest.TestCase):
    def test_adapter_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('adapter'), 'adapter implementation missing')


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_security_and_tools(self):
        from adapter import create_app, Settings
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        # These are deliberately public test-only credentials, not production secrets.
        settings = Settings('MOCK_KEY', {'profile-a': 'a'*40, 'profile-b': 'b'*40}, ['testserver'], [])
        calls = []
        def upstream(request):
            calls.append(request)
            if b'FAIL_FIXTURE' in request.content:
                return httpx.Response(500, text='MOCK_SECRET_DO_NOT_LEAK')
            return httpx.Response(200, json={'results': [{'title':'MOCK TITLE','url':'https://example.com','content':'x'*100000}], 'title':'MOCK TITLE','content':'fixture'})
        app = create_app(settings, httpx.MockTransport(upstream))
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
                for method in ['GET','POST','DELETE']:
                    for token in ['', 'wrong']:
                        r = await client.request(method, '/deepseek', headers={'Authorization': 'Bearer '+token})
                        self.assertEqual(r.status_code,401)
                for headers in [{'Host':'evil.example'}, {'Origin':'https://evil.example'}]:
                    r = await client.post('/deepseek', headers={'Authorization':'Bearer '+'a'*40, **headers})
                    self.assertEqual(r.status_code,403)
            for token in ['a'*40,'b'*40]:
                def factory(headers=None, timeout=None, auth=None):
                    return httpx.AsyncClient(transport=transport, headers=headers, timeout=timeout or 30, auth=auth)
                async with streamablehttp_client('http://testserver/deepseek',headers={'Authorization':'Bearer '+token},httpx_client_factory=factory) as (read,write,_):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertEqual({t.name for t in tools.tools},{'search','fetch'})
                        result = await session.call_tool('search',{'query':'fixture','count':1,'need_content':True})
                        self.assertFalse(result.isError)
                        self.assertLess(len(str(result)),40000)
                        result = await session.call_tool('fetch',{'url':'https://example.com'})
                        self.assertFalse(result.isError)
                        for name,args in [('search',{'query':''}),('search',{'query':'x'*129}),('search',{'query':'x','count':51}),('search',{'query':'x','count':True}),('fetch',{'url':'http://127.0.0.1'}),('fetch',{'url':'http://[::1]'}),('fetch',{'url':'http://user:pass@example.com'}),('fetch',{'url':'file:///etc/passwd'})]:
                            before = len(calls)
                            result = await session.call_tool(name,args)
                            self.assertTrue(result.isError,(name,args))
                            self.assertEqual(len(calls),before)
                        before = len(calls)
                        result = await session.call_tool('search',{'query':'FAIL_FIXTURE'})
                        self.assertTrue(result.isError)
                        self.assertNotIn('MOCK_SECRET',str(result))
                        self.assertEqual(len(calls),before+1)
        self.assertTrue(calls)
        self.assertTrue(all(str(r.url) in ['https://api.deepseek.com/v1/tools/search','https://api.deepseek.com/v1/tools/fetch'] for r in calls))

    async def test_empty_configuration_fails_closed(self):
        from adapter import Settings
        for key,tokens in [('',{}),('MOCK',{}),('MOCK',{'p':'short'}),('MOCK',{'p':'x'*40,'q':'x'*40})]:
            with self.assertRaises(ValueError):
                Settings(key,tokens,['testserver'],[])


if __name__ == '__main__':
    unittest.main()
