import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { SessionProvider } from "@/components/session";

import "./globals.css";

export const metadata: Metadata = {
  title: "ContextLedger",
  description: "What did an agent know when it decided, and was it valid and authorized then?",
};

const LINKS = [
  ["/search", "Search"],
  ["/entities", "Timeline"],
  ["/decisions", "Receipts"],
  ["/contradictions", "Contradictions"],
  ["/revocations", "Revocations"],
  ["/ask", "Ask"],
] as const;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <SessionProvider>
          <header>
            <nav>
              <strong>
                <Link href="/">ContextLedger</Link>
              </strong>
              {LINKS.map(([href, label]) => (
                <Link key={href} href={href}>
                  {label}
                </Link>
              ))}
            </nav>
          </header>
          <main>{children}</main>
        </SessionProvider>
      </body>
    </html>
  );
}
