import assert from 'node:assert/strict';
import test from 'node:test';
import { isWritingV1Preview, loadInitialProviderState } from '../src/hooks/useProviders.js';

function providerFixture() {
  const calls = [];
  const api = {
    providers: async () => { calls.push('GET /providers'); return { providers: [{ state: 'available' }] }; },
    validateConfiguredProviders: async () => { calls.push('POST /providers/validate-configured'); },
  };
  return { calls, refresh: () => api.providers(), poll: () => api.providers(), validate: () => api.validateConfiguredProviders() };
}

test('explicit writing V1 preview loads and polls provider state without paid validation', async () => {
  const fixture = providerFixture();
  await loadInitialProviderState({ ...fixture, autoValidate: !isWritingV1Preview('?reportPreview=deepseek&writingV1Preview=1') });
  assert.deepEqual(fixture.calls, ['GET /providers', 'GET /providers']);
});

test('normal initial load validates the configured provider before polling', async () => {
  const fixture = providerFixture();
  await loadInitialProviderState(fixture);
  assert.deepEqual(fixture.calls, ['GET /providers', 'POST /providers/validate-configured', 'GET /providers']);
});

test('explicit validation remains available when the caller requests it', async () => {
  const fixture = providerFixture();
  await loadInitialProviderState({ ...fixture, autoValidate: true });
  assert.deepEqual(fixture.calls, ['GET /providers', 'POST /providers/validate-configured', 'GET /providers']);
});

test('repeated preview loads never invoke a validation callback and still read fresh state', async () => {
  const fixture = providerFixture();
  const options = { ...fixture, autoValidate: false, validate: () => assert.fail('Preview validation is forbidden') };
  await loadInitialProviderState(options);
  await loadInitialProviderState(options);
  assert.deepEqual(fixture.calls, Array(4).fill('GET /providers'));
});

test('cleanup during the initial read prevents subsequent validation and polling', async () => {
  const fixture = providerFixture();
  let cancelled = false;
  await loadInitialProviderState({ ...fixture, refresh: async () => { await fixture.refresh(); cancelled = true; }, isCancelled: () => cancelled });
  assert.deepEqual(fixture.calls, ['GET /providers']);
});

test('failed local reads propagate without falling through to a validation request', async () => {
  const fixture = providerFixture();
  await assert.rejects(loadInitialProviderState({ ...fixture, refresh: async () => { throw new Error('Local read failed'); } }), /Local read failed/);
  assert.deepEqual(fixture.calls, []);
});
