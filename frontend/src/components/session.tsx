"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

import type { Session } from "@/lib/api";

const KEY = "contextledger.session";

interface SessionState {
  session: Session | null;
  save: (session: Session | null) => void;
}

const SessionContext = createContext<SessionState>({ session: null, save: () => undefined });

/** Keeps the organization and credentials in this browser only (localStorage). */
export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(KEY);
      if (raw) setSession(JSON.parse(raw) as Session);
    } catch {
      setSession(null);
    }
  }, []);

  function save(next: Session | null) {
    setSession(next);
    try {
      if (next) window.localStorage.setItem(KEY, JSON.stringify(next));
      else window.localStorage.removeItem(KEY);
    } catch {
      // Storage can be unavailable (private mode); the session then lasts for this tab.
    }
  }

  return <SessionContext.Provider value={{ session, save }}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionState {
  return useContext(SessionContext);
}
