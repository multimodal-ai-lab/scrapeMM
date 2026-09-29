// noVNC ships no type declarations. This covers the part of its RFB client that the
// CAPTCHA panel (components/VncPanel.vue) uses; see noVNC's docs/API.md for the rest.
declare module '@novnc/novnc' {
  export interface RFBDisconnectEvent extends CustomEvent<{ clean: boolean }> {}

  export default class RFB extends EventTarget {
    constructor(target: Element, urlOrChannel: string | WebSocket, options?: {
      shared?: boolean
      credentials?: { username?: string, password?: string, target?: string }
      repeaterID?: string
      wsProtocols?: string[]
    })

    scaleViewport: boolean
    clipViewport: boolean
    dragViewport: boolean
    viewOnly: boolean
    showDotCursor: boolean

    disconnect(): void
  }
}
