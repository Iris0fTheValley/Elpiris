import {QueryClient} from '@tanstack/react-query';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Navigation should reuse recently fetched editorial data. Active jobs
      // declare their own polling interval and mutations explicitly invalidate
      // affected keys, so a longer freshness window does not hide live progress.
      staleTime: 30_000,
      gcTime: 15 * 60_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
    mutations: {
      retry: false,
    },
  },
});
