"use client";

import { useEffect } from "react";
import { trackPageVisit } from "@/lib/track";

export default function PageTracker() {
  useEffect(() => {
    trackPageVisit(window.location.pathname);
  }, []);
  return null;
}
