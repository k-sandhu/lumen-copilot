import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { headers } from "next/headers";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host =
    requestHeaders.get("x-forwarded-host") ??
    requestHeaders.get("host") ??
    "localhost:3000";
  const protocol =
    requestHeaders.get("x-forwarded-proto") ??
    (host.startsWith("localhost") ? "http" : "https");
  const origin = `${protocol}://${host}`;

  return {
    metadataBase: new URL(origin),
    title: "Lumen Backend Explorer",
    description:
      "An interactive, code-grounded map of Lumen Copilot's backend data flow, call stack, boundaries, and containers.",
    openGraph: {
      title: "Lumen Backend Explorer",
      description: "See what calls what — from HTTP request to grounded WebSocket answer.",
      type: "website",
      url: origin,
      images: [
        {
          url: `${origin}/og.png`,
          width: 1792,
          height: 938,
          alt: "Lumen Backend Explorer flow from Browser through FastAPI, ChatRuntime, Retrieval, Redis, and WebSocket",
        },
      ],
    },
    twitter: {
      card: "summary_large_image",
      title: "Lumen Backend Explorer",
      description: "See what calls what — from HTTP request to grounded WebSocket answer.",
      images: [`${origin}/og.png`],
    },
  };
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className={`${geistSans.variable} ${geistMono.variable}`}>
        {children}
      </body>
    </html>
  );
}
