import type { Metadata } from "next";
import "./globals.css";
import localFont from "next/font/local";
import { ThemeProvider } from "@/components/theme-provider";

// Self-hosted variable fonts. Files live under web/frontend/app/fonts/ so
// `next/font/local` can resolve them without leaving the `app/` boundary
// (Turbopack rejects `..` traversal) and so the Docker build does not need
// to reach fonts.googleapis.com.
const inter = localFont({
  src: [
    {
      path: "./fonts/Inter-VariableFont.woff2",
      style: "normal",
      weight: "100 900",
    },
    {
      path: "./fonts/Inter-Italic-VariableFont.woff2",
      style: "italic",
      weight: "100 900",
    },
  ],
  variable: "--font-ui",
  display: "swap",
});

const jetbrainsMono = localFont({
  src: [
    {
      path: "./fonts/JetBrainsMono-VariableFont.woff2",
      style: "normal",
      weight: "100 800",
    },
    {
      path: "./fonts/JetBrainsMono-Italic-VariableFont.woff2",
      style: "italic",
      weight: "100 800",
    },
  ],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "CI Failure Analyzer",
  description: "Dashboard for Jenkins failure analytics",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${inter.variable} ${jetbrainsMono.variable} antialiased`}>
        <ThemeProvider attribute="class" defaultTheme="dark" enableSystem disableTransitionOnChange>
          {children}
        </ThemeProvider>
      </body>
    </html>
  );
}
