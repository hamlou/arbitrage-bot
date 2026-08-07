"use client";

import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useLiveWs } from "@/lib/useLive";

function LiveBridge() {
  useLiveWs();
  return null;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            retry: 2,
            refetchOnWindowFocus: false,
            staleTime: 2000,
          },
        },
      })
  );

  return (
    <QueryClientProvider client={client}>
      <LiveBridge />
      {children}
    </QueryClientProvider>
  );
}
