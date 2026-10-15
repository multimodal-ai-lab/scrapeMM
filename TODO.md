# To do

## gov.il integration
www.gov.il pages (e.g. https://www.gov.il/en/pages/event_fly261120) are a React app behind
Cloudflare; the browser sometimes renders them empty on the server. The content comes from a
public JSON API that answers plain HTTP (no Cloudflare):

```
GET https://openapi-gc.digital.gov.il/pub/cio/govil/rest/contentpage/v1/api/content-pages/{urlName}?culture={en|he|ar|ru|fr|es}
Headers: x-client-id: <govilRunConfig.clientId>, Origin: https://www.gov.il
```

- Without both headers: HTTP 500 "Global Error Message".
- The client id is public, in `https://www.gov.il/ContentpageWebApi/client-config.js`
  (`window['govilRunConfig']={..., "clientId": "..."}`; behind Cloudflare for aiohttp, loads
  with curl_cffi): fetch once and cache, with the current value as fallback.
- JSON: `contentHead.title`, `contentMain.htmlContents[i].sectionData` (the body as HTML),
  `contentMain.images[i].sectionData.imageObj.src`.
- A small integration like `integrations/ncbi.py`: `domains=["gov.il"]`, handle
  `^/(he|en|ar|ru|fr|es)/pages/<name>` via the API, the browser (`super()._get()`) otherwise;
  build the content with `to_scraped_content(html, ...)`.
