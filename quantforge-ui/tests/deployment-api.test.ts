import { afterEach, expect, it, vi } from 'vitest'
afterEach(() => { vi.unstubAllEnvs(); vi.resetModules() })
it('uses the same-origin gateway for the report API', async () => {
  vi.stubEnv('VITE_QF_BINDINGS_API', '/gateway')
  expect((await import('../src/data/chartApi')).chartApi).toBe('/gateway')
})
it.each(['//foreign.example', '/gateway/../foreign', 'https://foreign.example', '/arbitrary'])('rejects unrecognized deployment targets: %s', async value => {
  vi.stubEnv('VITE_QF_BINDINGS_API', value)
  await expect(import('../src/data/chartApi')).rejects.toThrow()
})
