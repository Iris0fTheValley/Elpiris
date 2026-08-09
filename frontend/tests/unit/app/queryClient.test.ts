import {describe, expect, it} from 'vitest';

import {queryClient} from '@/app/queryClient';

describe('queryClient cache policy', () => {
  it('retains navigation data while explicit polling owns live refreshes', () => {
    const options = queryClient.getDefaultOptions().queries;

    expect(options?.staleTime).toBe(30_000);
    expect(options?.gcTime).toBe(15 * 60_000);
    expect(options?.refetchOnWindowFocus).toBe(false);
  });
});
