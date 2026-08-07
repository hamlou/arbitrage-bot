import type { Metadata, Viewport } from "next";
import { Providers } from "./providers";
import { AppShell } from "@/components/layout/app-shell";
import "./globals.css";

export const metadata: Metadata = {
  title: "Arb OS — Command Center",
  description: "Live command center for the Polymarket arbitrage bot — trades, positions, latency, risk and markets in one place.",
};

export const viewport: Viewport = {
  themeColor: "#09090E",
};

const THEME_INIT = `
try {
  var t = localStorage.getItem("theme");
  if (t === "light") document.documentElement.classList.add("light");
} catch (e) {}
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT }} />
      </head>
      <body>
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
