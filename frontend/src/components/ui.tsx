"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import { ApiError } from "@/lib/api";

import { useSession } from "./session";

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const message =
    error instanceof ApiError
      ? `${error.status} ${error.problem.code ?? ""}: ${error.message}`
      : error instanceof Error
        ? error.message
        : String(error);
  const correlation = error instanceof ApiError ? error.problem.correlation_id : undefined;
  return (
    <div className="error" role="alert">
      {message}
      {correlation ? <div className="muted">correlation id {correlation}</div> : null}
    </div>
  );
}

export function Badge({ tone, children }: { tone: "ok" | "warn" | "bad" | "info"; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

/** Renders children only once an organization and credentials are configured. */
export function RequireSession({ children }: { children: ReactNode }) {
  const { session } = useSession();
  if (!session) {
    return (
      <p className="muted">
        Set your organization and credentials on the <Link href="/">connection page</Link> first.
      </p>
    );
  }
  return <>{children}</>;
}
