import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "CI Failure Analyzer",
  description: "Dashboard for Jenkins failure analytics",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
