/**
 * virt-glass native shim
 *
 * Wraps the Looking Glass LGMP transport (vendored from the Looking Glass
 * client) behind a small, ctypes-friendly C API.  All the protocol work is
 * done by the upstream transport so that we stay wire-compatible with the
 * Looking Glass host application running in the guest.
 *
 * Threads:
 *   session  - creates/connects the transport, restarts it on loss
 *   frame    - pulls frames, copies them into a triple buffer
 *   pointer  - pulls cursor updates, converts shapes to BGRA
 *   clip     - drives clipboard operations outside of event callbacks
 *
 * Python is woken through an eventfd whenever a frame, cursor, state or
 * clipboard event is available.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/eventfd.h>
#include <time.h>
#include <unistd.h>

#include "common/debug.h"
#include "common/framebuffer.h"
#include "common/option.h"
#include "interface/transport.h"

extern const LG_TransportOps LGT_LGMP;

#define VG_EXPORT __attribute__((visibility("default")))

enum
{
  VG_STATE_IDLE         = 0, /* not started                         */
  VG_STATE_NO_DEVICE    = 1, /* shm device can't be opened          */
  VG_STATE_WAITING      = 2, /* device ok, waiting for host app     */
  VG_STATE_CONNECTED    = 3, /* session established                 */
  VG_STATE_BAD_VERSION  = 4, /* host/client protocol mismatch       */
};

enum
{
  VG_EVENT_FRAME     = 1,
  VG_EVENT_CURSOR    = 2,
  VG_EVENT_STATE     = 4,
  VG_EVENT_CLIPBOARD = 8,
  VG_EVENT_INPUT     = 16,
};

typedef struct VGSessionInfo
{
  char     version[32];
  char     osName[64];
  char     capture[32];
  char     cpuModel[256];
  uint32_t features;
  uint32_t os;
  uint8_t  cpus, cores, sockets, uuidValid;
  uint8_t  uuid[16];
  uint32_t expectedVersion;
  uint32_t currentVersion;
  char     mismatchComponent[16];
}
VGSessionInfo;

typedef struct VGFrameInfo
{
  uint64_t serial;
  uint32_t type;
  uint32_t screenWidth, screenHeight;
  uint32_t frameWidth, frameHeight;
  uint32_t dataWidth, dataHeight;
  uint32_t stride, pitch;
  uint32_t rotation;
  uint32_t hdr, hdrPQ;
  uint32_t flags;
  uint32_t sdrWhiteLevel;
  uint64_t size;
}
VGFrameInfo;

typedef struct VGCursorInfo
{
  uint64_t shapeSerial;   /* bumps when the shape changes            */
  uint64_t posSerial;     /* bumps when the position changes         */
  int32_t  x, y;          /* guest position of the hotspot           */
  int32_t  hx, hy;        /* hotspot                                 */
  uint32_t width, height; /* converted BGRA image dimensions         */
  uint32_t visible;
  uint32_t type;          /* original KVMFR cursor type              */
}
VGCursorInfo;

typedef struct FrameBuf
{
  uint8_t   * data;
  size_t      cap;
  VGFrameInfo info;
}
FrameBuf;

#define CLIP_MAX_TYPES 8

typedef struct ClipOutgoing
{
  bool             active;
  LG_ClipboardRequest request;
  LG_ClipboardData type;
  uint8_t        * data;
  size_t           size;
  size_t           offset;
  int              stage;   /* 0 begin, 1 chunks, 2 end */
  bool             blocked; /* waiting for dataReady        */
}
ClipOutgoing;

static struct
{
  bool                    setup;
  char                    shmPath[512];
  int                     efd;
  atomic_uint             events;

  pthread_t               sessionThread;
  atomic_bool             running;
  atomic_int              state;

  pthread_mutex_t         lock;      /* protects transport handle & ops */
  LG_Transport          * transport;
  const LG_VideoOps     * video;
  VGSessionInfo           session;

  pthread_t               frameThread, pointerThread;
  /* frame scheduling: keep the host delivering frames at our refresh rate */
  atomic_ullong           schedPeriodNs;
  atomic_bool             schedImmediate;
  uint32_t                schedGeneration;
  bool                    schedPending;
  bool                    schedActive;
  LG_TransportControlToken schedToken;
  uint64_t                schedLastSend;
  atomic_bool             videoRunning;
  atomic_bool             lost;

  /* triple buffer: [write] [ready] [display] */
  pthread_mutex_t         fbLock;
  FrameBuf                fb[3];
  int                     fbWrite, fbReady, fbDisplay;
  bool                    fbFresh;
  atomic_ullong           frameCount;

  /* cursor */
  pthread_mutex_t         curLock;
  VGCursorInfo            cursor;
  uint8_t               * curData;
  size_t                  curCap;

  /* input */
  pthread_mutex_t         inLock;
  const LG_InputOps     * inOps;
  void                  * inOpaque;
  LG_InputStatus          inStatus;     /* protected by stLock */
  pthread_mutex_t         stLock;
  atomic_bool             inListening;

  /* clipboard */
  pthread_mutex_t         clLock;
  pthread_cond_t          clCond;
  pthread_t               clipThread;
  const LG_ClipboardOps * clOps;
  void                  * clOpaque;
  atomic_bool             clStatusAvail; /* guest clipboard available */
  atomic_bool             clAttached;    /* event ops attached        */
  bool                    clListening;
  /* guest -> host */
  LG_ClipboardData        clGuestTypes[CLIP_MAX_TYPES];
  int                     clGuestTypeCount;
  uint64_t                clGuestSerial;
  LG_ClipboardRequest     clInRequest;
  LG_ClipboardData        clInType;
  uint8_t               * clInData;
  size_t                  clInSize, clInCap;
  bool                    clInDone;
  uint64_t                clInSerial;
  /* host -> guest */
  LG_ClipboardRequest     clGuestRequest;     /* pending guest request */
  LG_ClipboardData        clGuestRequestType;
  uint64_t                clGuestRequestSerial;
  ClipOutgoing            clOut;
  bool                    clOutReady;
  LG_ClipboardRequest     clNextRequest;
  /* commands for the clip thread */
  int                     clCmdNotifyCount;   /* -1 = none, 0 = release */
  LG_ClipboardData        clCmdNotify[CLIP_MAX_TYPES];
  bool                    clCmdRequest;
  LG_ClipboardRequest     clCmdCancel;        /* guest request to refuse */
  LG_ClipboardData        clCmdRequestType;
}
g = { .efd = -1 };

static void vg_signal(unsigned ev)
{
  atomic_fetch_or(&g.events, ev);
  if (g.efd >= 0)
  {
    uint64_t one = 1;
    ssize_t r = write(g.efd, &one, sizeof(one));
    (void)r;
  }
}

static void vg_sleep_ms(unsigned ms)
{
  struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
  nanosleep(&ts, NULL);
}

/* ------------------------------------------------------------------------ */
/* frames                                                                    */

static void * frameThreadFn(void * unused)
{
  (void)unused;
  const LG_FrameOps * ops = g.video->frame;

  while (atomic_load(&g.videoRunning))
  {
    LG_TransportFrame frame;
    LG_TransportStatus status = ops->nextFrame(g.transport, false, &frame);
    if (status != LG_TRANSPORT_OK)
    {
      if (status == LG_TRANSPORT_TIMEOUT || status == LG_TRANSPORT_UNAVAILABLE)
        continue;
      if (status == LG_TRANSPORT_ERROR)
      {
        vg_sleep_ms(10);
        continue;
      }
      atomic_store(&g.lost, true);
      vg_signal(VG_EVENT_STATE);
      break;
    }

    const LG_TransportFrameFormat * fmt = frame.format;
    size_t size = 0;
    if (!fmt || !frame.framebuffer ||
        !lgTransport_validateFrameFormat(fmt, frame.flags, &size))
    {
      ops->releaseFrame(g.transport, &frame);
      continue;
    }

    FrameBuf * fb = &g.fb[g.fbWrite];
    if (fb->cap < size)
    {
      free(fb->data);
      fb->data = aligned_alloc(64, (size + 63) & ~(size_t)63);
      fb->cap  = fb->data ? size : 0;
      if (!fb->data)
      {
        ops->releaseFrame(g.transport, &frame);
        continue;
      }
    }

    const bool ok = framebuffer_read_linear(frame.framebuffer, fb->data, size);
    if (ok)
    {
      fb->info = (VGFrameInfo)
      {
        .serial        = frame.serial,
        .type          = fmt->type,
        .screenWidth   = fmt->screenWidth,
        .screenHeight  = fmt->screenHeight,
        .frameWidth    = fmt->frameWidth,
        .frameHeight   = fmt->frameHeight,
        .dataWidth     = fmt->dataWidth,
        .dataHeight    = fmt->dataHeight,
        .stride        = fmt->stride,
        .pitch         = fmt->pitch,
        .rotation      = fmt->rotation,
        .hdr           = fmt->hdr,
        .hdrPQ         = fmt->hdrPQ,
        .flags         = frame.flags,
        .sdrWhiteLevel = fmt->sdrWhiteLevel,
        .size          = size,
      };
    }
    ops->releaseFrame(g.transport, &frame);

    if (!ok)
      continue;

    pthread_mutex_lock(&g.fbLock);
    const int tmp = g.fbReady;
    g.fbReady  = g.fbWrite;
    g.fbWrite  = tmp;
    g.fbFresh  = true;
    pthread_mutex_unlock(&g.fbLock);
    atomic_fetch_add(&g.frameCount, 1);
    vg_signal(VG_EVENT_FRAME);
  }

  if (ops->stopFrame)
    ops->stopFrame(g.transport);
  return NULL;
}

/* ------------------------------------------------------------------------ */
/* cursor                                                                    */

static bool cursorEnsure(size_t size)
{
  if (g.curCap >= size)
    return true;
  uint8_t * n = realloc(g.curData, size);
  if (!n)
    return false;
  g.curData = n;
  g.curCap  = size;
  return true;
}

/* convert the guest cursor shape into straight-alpha BGRA */
static void cursorConvert(const LG_TransportPointer * p)
{
  uint32_t w = p->width, h = p->height;
  if (p->type == CURSOR_TYPE_MONOCHROME)
    h /= 2;

  if (!w || !h || w > 512 || h > 512 || !cursorEnsure((size_t)w * h * 4))
    return;

  uint32_t * out = (uint32_t *)g.curData;
  switch (p->type)
  {
    case CURSOR_TYPE_COLOR:
      for (uint32_t y = 0; y < h; ++y)
        memcpy(out + y * w, p->shape + y * p->pitch, w * 4);
      break;

    case CURSOR_TYPE_MASKED_COLOR:
      for (uint32_t y = 0; y < h; ++y)
      {
        const uint32_t * src = (const uint32_t *)(p->shape + y * p->pitch);
        for (uint32_t x = 0; x < w; ++x)
        {
          const uint32_t px  = src[x];
          const uint32_t rgb = px & 0x00FFFFFF;
          if (!(px & 0xFF000000))
            out[y * w + x] = rgb | 0xFF000000;        /* opaque colour */
          else if (!rgb)
            out[y * w + x] = 0;                       /* transparent   */
          else
            out[y * w + x] = 0xC0000000;              /* xor -> shade  */
        }
      }
      break;

    case CURSOR_TYPE_MONOCHROME:
    {
      const uint8_t * andMask = p->shape;
      const uint8_t * xorMask = p->shape + h * p->pitch;
      for (uint32_t y = 0; y < h; ++y)
        for (uint32_t x = 0; x < w; ++x)
        {
          const int bit = 7 - (x & 7);
          const int a = (andMask[y * p->pitch + x / 8] >> bit) & 1;
          const int b = (xorMask[y * p->pitch + x / 8] >> bit) & 1;
          uint32_t px;
          if (!a && !b)     px = 0xFF000000;   /* black       */
          else if (!a && b) px = 0xFFFFFFFF;   /* white       */
          else if (a && !b) px = 0x00000000;   /* transparent */
          else              px = 0x00FFFFFF;   /* invert (marker) */
          out[y * w + x] = px;
        }
      /* XOR pixels can't be expressed with alpha: draw them white and give
       * them a black outline so they stay visible on any background */
      for (uint32_t y = 0; y < h; ++y)
        for (uint32_t x = 0; x < w; ++x)
        {
          if (out[y * w + x] != 0x00FFFFFF)
            continue;
          out[y * w + x] = 0xFFFFFFFF;
          for (int dy = -1; dy <= 1; ++dy)
            for (int dx = -1; dx <= 1; ++dx)
            {
              const int nx = (int)x + dx, ny = (int)y + dy;
              if (nx < 0 || ny < 0 || nx >= (int)w || ny >= (int)h)
                continue;
              uint32_t * n = &out[ny * w + nx];
              if (*n == 0)
                *n = 0xFF000000;
            }
        }
      break;
    }

    default:
      return;
  }

  g.cursor.width  = w;
  g.cursor.height = h;
  g.cursor.hx     = p->hx;
  g.cursor.hy     = p->hy;
  g.cursor.type   = p->type;
  g.cursor.shapeSerial++;
}

static void * pointerThreadFn(void * unused)
{
  (void)unused;
  const LG_FrameOps * ops = g.video->frame;

  while (atomic_load(&g.videoRunning))
  {
    LG_TransportPointer p;
    LG_TransportStatus status = ops->nextPointer(g.transport, &p);
    if (status != LG_TRANSPORT_OK)
    {
      if (status == LG_TRANSPORT_TIMEOUT || status == LG_TRANSPORT_UNAVAILABLE)
        continue;
      if (status == LG_TRANSPORT_ERROR)
      {
        vg_sleep_ms(10);
        continue;
      }
      atomic_store(&g.lost, true);
      vg_signal(VG_EVENT_STATE);
      break;
    }

    pthread_mutex_lock(&g.curLock);
    if (p.flags & LG_TRANSPORT_POINTER_VISIBLE_VALID)
      g.cursor.visible = (p.flags & LG_TRANSPORT_POINTER_VISIBLE) ? 1 : 0;
    if ((p.flags & LG_TRANSPORT_POINTER_SHAPE) && p.shape)
      cursorConvert(&p);
    if (p.flags & LG_TRANSPORT_POINTER_POSITION)
    {
      g.cursor.x = p.x;
      g.cursor.y = p.y;
      g.cursor.posSerial++;
    }
    pthread_mutex_unlock(&g.curLock);

    ops->releasePointer(g.transport, &p);
    vg_signal(VG_EVENT_CURSOR);
  }

  if (ops->stopPointer)
    ops->stopPointer(g.transport);
  return NULL;
}

/* ------------------------------------------------------------------------ */
/* input                                                                     */

/* Runs on a backend thread. Uses its own lock rather than inLock: an input
 * operation called under inLock may deliver status synchronously. */
static void inputStatusFn(void * opaque, const LG_InputStatus * status)
{
  (void)opaque;
  if (!atomic_load(&g.inListening))
    return;
  pthread_mutex_lock(&g.stLock);
  g.inStatus = *status;
  pthread_mutex_unlock(&g.stLock);
  vg_signal(VG_EVENT_INPUT);
}

/* ------------------------------------------------------------------------ */
/* clipboard                                                                 */

static void clipFreeOut(void)
{
  free(g.clOut.data);
  memset(&g.clOut, 0, sizeof(g.clOut));
}

static void evNotice(void * o, const LG_ClipboardData types[], size_t count)
{
  (void)o;
  pthread_mutex_lock(&g.clLock);
  g.clGuestTypeCount = 0;
  for (size_t i = 0; i < count && i < CLIP_MAX_TYPES; ++i)
    g.clGuestTypes[g.clGuestTypeCount++] = types[i];
  g.clGuestSerial++;
  pthread_mutex_unlock(&g.clLock);
  vg_signal(VG_EVENT_CLIPBOARD);
}

static void evRelease(void * o)
{
  (void)o;
  pthread_mutex_lock(&g.clLock);
  g.clGuestTypeCount = 0;
  g.clGuestSerial++;
  pthread_mutex_unlock(&g.clLock);
  vg_signal(VG_EVENT_CLIPBOARD);
}

static bool clipInAppend(const void * data, size_t size)
{
  if (g.clInSize + size > g.clInCap)
  {
    size_t cap = g.clInCap ? g.clInCap : 65536;
    while (cap < g.clInSize + size)
      cap *= 2;
    if (cap > 256u * 1024 * 1024)
      return false;
    uint8_t * n = realloc(g.clInData, cap);
    if (!n)
      return false;
    g.clInData = n;
    g.clInCap  = cap;
  }
  memcpy(g.clInData + g.clInSize, data, size);
  g.clInSize += size;
  return true;
}

static void evData(void * o, LG_ClipboardRequest request,
    LG_ClipboardData type, const void * data, size_t size)
{
  (void)o;
  pthread_mutex_lock(&g.clLock);
  if (request == g.clInRequest)
  {
    g.clInSize = 0;
    g.clInType = type;
    if (clipInAppend(data, size))
    {
      g.clInDone = true;
      g.clInSerial++;
    }
  }
  pthread_mutex_unlock(&g.clLock);
  vg_signal(VG_EVENT_CLIPBOARD);
}

static LG_ClipboardResult evDataBegin(void * o, LG_ClipboardRequest request,
    LG_ClipboardData type, uint64_t sizeHint)
{
  (void)o; (void)sizeHint;
  LG_ClipboardResult r = LG_CLIPBOARD_RESULT_FAILED;
  pthread_mutex_lock(&g.clLock);
  if (request == g.clInRequest)
  {
    g.clInType = type;
    g.clInSize = 0;
    r = LG_CLIPBOARD_RESULT_ACCEPTED;
  }
  pthread_mutex_unlock(&g.clLock);
  return r;
}

static LG_ClipboardResult evDataChunk(void * o, LG_ClipboardRequest request,
    uint64_t offset, const void * data, size_t size)
{
  (void)o;
  LG_ClipboardResult r = LG_CLIPBOARD_RESULT_FAILED;
  pthread_mutex_lock(&g.clLock);
  if (request == g.clInRequest && offset == g.clInSize &&
      clipInAppend(data, size))
    r = LG_CLIPBOARD_RESULT_ACCEPTED;
  pthread_mutex_unlock(&g.clLock);
  return r;
}

static LG_ClipboardResult evDataEnd(void * o, LG_ClipboardRequest request,
    uint64_t finalSize)
{
  (void)o;
  LG_ClipboardResult r = LG_CLIPBOARD_RESULT_FAILED;
  pthread_mutex_lock(&g.clLock);
  if (request == g.clInRequest && finalSize == g.clInSize)
  {
    g.clInDone = true;
    g.clInSerial++;
    r = LG_CLIPBOARD_RESULT_ACCEPTED;
  }
  pthread_mutex_unlock(&g.clLock);
  if (r == LG_CLIPBOARD_RESULT_ACCEPTED)
    vg_signal(VG_EVENT_CLIPBOARD);
  return r;
}

static void evDataCancel(void * o, LG_ClipboardRequest request,
    LG_ClipboardCancelReason reason)
{
  (void)o; (void)reason;
  pthread_mutex_lock(&g.clLock);
  if (request == g.clInRequest)
  {
    g.clInRequest = LG_CLIPBOARD_REQUEST_INVALID;
    g.clInSize    = 0;
  }
  pthread_mutex_unlock(&g.clLock);
}

static void evDataReady(void * o, LG_ClipboardRequest request)
{
  (void)o;
  pthread_mutex_lock(&g.clLock);
  if (g.clOut.active && g.clOut.request == request)
  {
    g.clOutReady = true;
    pthread_cond_signal(&g.clCond);
  }
  pthread_mutex_unlock(&g.clLock);
}

static void evRequestCancel(void * o, LG_ClipboardRequest request,
    LG_ClipboardCancelReason reason)
{
  (void)o; (void)reason;
  pthread_mutex_lock(&g.clLock);
  if (g.clGuestRequest == request)
    g.clGuestRequest = LG_CLIPBOARD_REQUEST_INVALID;
  if (g.clOut.active && g.clOut.request == request)
    clipFreeOut();
  pthread_mutex_unlock(&g.clLock);
}

static bool evRequest(void * o, LG_ClipboardRequest request,
    LG_ClipboardData type)
{
  (void)o;
  pthread_mutex_lock(&g.clLock);
  g.clGuestRequest     = request;
  g.clGuestRequestType = type;
  g.clGuestRequestSerial++;
  pthread_mutex_unlock(&g.clLock);
  vg_signal(VG_EVENT_CLIPBOARD);
  return true;
}

/* File datasets are never offered by this client, so the guest has nothing to
 * acquire; guest file offers are ignored (files go through the guest agent
 * instead). */
static void evFileOffer(void * o, uint64_t d) { (void)o; (void)d; }
static void evFileAcquire(void * o, uint64_t d, uint64_t a)
  { (void)o; (void)d; (void)a; }
static void evFileAcquired(void * o, uint64_t d, uint64_t a,
    LG_ClipboardFileError e) { (void)o; (void)d; (void)a; (void)e; }
static void evFileRelease(void * o, uint64_t d, uint64_t a)
  { (void)o; (void)d; (void)a; }
static void evFileRequest(void * o, const LG_ClipboardFileRequest * r)
  { (void)o; (void)r; }
static LG_ClipboardResult evFileDataBegin(void * o,
    const LG_ClipboardFileRequest * r, uint64_t s)
  { (void)o; (void)r; (void)s; return LG_CLIPBOARD_RESULT_FAILED; }
static LG_ClipboardResult evFileDataChunk(void * o,
    const LG_ClipboardFileRequest * r, uint64_t off, const void * d, size_t s)
  { (void)o; (void)r; (void)off; (void)d; (void)s;
    return LG_CLIPBOARD_RESULT_FAILED; }
static LG_ClipboardResult evFileDataEnd(void * o,
    const LG_ClipboardFileRequest * r, uint64_t s)
  { (void)o; (void)r; (void)s; return LG_CLIPBOARD_RESULT_FAILED; }
static void evFileDataReady(void * o, uint64_t r) { (void)o; (void)r; }
static void evFileCancel(void * o, uint64_t d, uint64_t r,
    LG_ClipboardFileError e) { (void)o; (void)d; (void)r; (void)e; }

static const LG_ClipboardEventOps CLIP_EVENTS =
{
  .notice        = evNotice,
  .data          = evData,
  .dataBegin     = evDataBegin,
  .dataChunk     = evDataChunk,
  .dataEnd       = evDataEnd,
  .dataCancel    = evDataCancel,
  .dataReady     = evDataReady,
  .requestCancel = evRequestCancel,
  .release       = evRelease,
  .request       = evRequest,
  .fileOffer     = evFileOffer,
  .fileAcquire   = evFileAcquire,
  .fileAcquired  = evFileAcquired,
  .fileRelease   = evFileRelease,
  .fileRequest   = evFileRequest,
  .fileDataBegin = evFileDataBegin,
  .fileDataChunk = evFileDataChunk,
  .fileDataEnd   = evFileDataEnd,
  .fileDataReady = evFileDataReady,
  .fileCancel    = evFileCancel,
};

static void clipStatusFn(void * opaque, const LG_ClipboardStatus * status)
{
  (void)opaque;
  atomic_store(&g.clStatusAvail, status->available);
  vg_signal(VG_EVENT_CLIPBOARD);
}

/* Attach the clipboard event ops once the guest side reports itself
 * available; attach fails before then. Called from the session thread. */
static void clipMaybeAttach(void)
{
  if (!g.clOps || atomic_load(&g.clAttached) ||
      !atomic_load(&g.clStatusAvail))
    return;
  if (g.clOps->attach(g.clOpaque, &CLIP_EVENTS, NULL))
  {
    atomic_store(&g.clAttached, true);
    vg_signal(VG_EVENT_CLIPBOARD);
  }
}

#define CLIP_CHUNK 65536

/* push as much of the outgoing transfer as the stream accepts */
static void clipPump(const LG_ClipboardOps * ops, void * opaque)
{
  ClipOutgoing * out = &g.clOut;
  while (out->active)
  {
    LG_ClipboardResult r;
    if (out->stage == 0)
      r = ops->dataBegin(opaque, out->request, out->type, out->size);
    else if (out->stage == 1)
    {
      if (out->offset >= out->size)
      {
        out->stage = 2;
        continue;
      }
      size_t n = out->size - out->offset;
      if (n > CLIP_CHUNK)
        n = CLIP_CHUNK;
      r = ops->dataChunk(opaque, out->request, out->offset,
          out->data + out->offset, n);
      if (r == LG_CLIPBOARD_RESULT_ACCEPTED)
      {
        out->offset += n;
        continue;
      }
    }
    else
      r = ops->dataEnd(opaque, out->request, out->size);

    if (r == LG_CLIPBOARD_RESULT_BLOCKED)
    {
      out->blocked = true; /* retry only after dataReady */
      return;
    }
    if (r == LG_CLIPBOARD_RESULT_FAILED)
    {
      /* release the guest's request slot so later pastes still work */
      ops->dataCancel(opaque, out->request, LG_CLIPBOARD_CANCEL_ABORTED);
      clipFreeOut();
      return;
    }
    if (out->stage == 2)
    {
      clipFreeOut();
      return;
    }
    out->stage++;
  }
}

static void * clipThreadFn(void * unused)
{
  (void)unused;
  pthread_mutex_lock(&g.clLock);
  while (atomic_load(&g.videoRunning))
  {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ts.tv_nsec += 100 * 1000000L;
    if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
    pthread_cond_timedwait(&g.clCond, &g.clLock, &ts);

    const LG_ClipboardOps * ops = g.clOps;
    void * opaque = g.clOpaque;
    if (!ops || !atomic_load(&g.clAttached))
      continue;

    if (g.clCmdCancel != LG_CLIPBOARD_REQUEST_INVALID)
    {
      const LG_ClipboardRequest req = g.clCmdCancel;
      g.clCmdCancel = LG_CLIPBOARD_REQUEST_INVALID;
      ops->dataCancel(opaque, req, LG_CLIPBOARD_CANCEL_ABORTED);
    }

    if (g.clCmdNotifyCount >= 0)
    {
      LG_ClipboardData types[CLIP_MAX_TYPES];
      const int count = g.clCmdNotifyCount;
      memcpy(types, g.clCmdNotify, sizeof(types));
      g.clCmdNotifyCount = -1;
      pthread_mutex_unlock(&g.clLock);
      if (count == 0)
        ops->release(opaque);
      else
        ops->notifyTypes(opaque, types, count);
      pthread_mutex_lock(&g.clLock);
    }

    if (g.clCmdRequest)
    {
      g.clCmdRequest = false;
      LG_ClipboardRequest req = ++g.clNextRequest;
      g.clInRequest = req;
      g.clInSize    = 0;
      g.clInDone    = false;
      const LG_ClipboardData type = g.clCmdRequestType;
      pthread_mutex_unlock(&g.clLock);
      const bool ok = ops->request(opaque, req, type);
      pthread_mutex_lock(&g.clLock);
      if (!ok && g.clInRequest == req)
        g.clInRequest = LG_CLIPBOARD_REQUEST_INVALID;
    }

    /* The stream operations never deliver event callbacks synchronously and
     * callbacks run without backend locks held, so pumping under clLock
     * cannot deadlock and keeps clOut consistent with requestCancel. */
    if (g.clOut.active && (!g.clOut.blocked || g.clOutReady))
    {
      g.clOutReady    = false;
      g.clOut.blocked = false;
      clipPump(ops, opaque);
    }
  }
  pthread_mutex_unlock(&g.clLock);
  return NULL;
}

/* ------------------------------------------------------------------------ */
/* frame scheduling                                                          */

static uint64_t nowNs(void)
{
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

#define SCHED_LEASE_MS 1000U
#define SCHED_RENEW_NS 250000000ULL

/* called from the session thread while connected */
static void schedUpdate(bool release)
{
  if (!(g.session.features & LG_TRANSPORT_FEATURE_FRAME_SCHEDULE))
    return;

  if (g.schedPending)
  {
    const LG_TransportStatus st =
      LGT_LGMP.controlStatus(g.transport, g.schedToken);
    if (st == LG_TRANSPORT_UNAVAILABLE && !release)
      return; /* not yet acknowledged */
    g.schedPending = false;
  }

  const uint64_t now    = nowNs();
  const uint64_t period = atomic_load(&g.schedPeriodNs);
  const bool immediate  = atomic_exchange(&g.schedImmediate, false);

  LG_TransportFrameScheduleFlags flags;
  if (release)
  {
    if (!g.schedActive)
      return;
    flags = LG_TRANSPORT_FRAME_SCHEDULE_RELEASE;
  }
  else
  {
    if (g.schedActive && !immediate && now - g.schedLastSend < SCHED_RENEW_NS)
      return;
    flags = LG_TRANSPORT_FRAME_SCHEDULE_ACTIVE;
    if (!g.schedActive || immediate)
    {
      flags |= LG_TRANSPORT_FRAME_SCHEDULE_RESET |
               LG_TRANSPORT_FRAME_SCHEDULE_IMMEDIATE;
      ++g.schedGeneration;
    }
  }

  const LG_TransportControl c =
  {
    .type = LG_TRANSPORT_CONTROL_FRAME_SCHEDULE,
    .frameSchedule =
    {
      .generation  = g.schedGeneration,
      .flags       = flags,
      .period      = release ? 0 : period,
      .targetSlack = 500000ULL,
      .lease       = SCHED_LEASE_MS,
    },
  };

  pthread_mutex_lock(&g.lock);
  const LG_TransportStatus st =
    LGT_LGMP.sendControl(g.transport, &c, &g.schedToken);
  pthread_mutex_unlock(&g.lock);
  if (st != LG_TRANSPORT_OK)
  {
    if (immediate)
      atomic_store(&g.schedImmediate, true);
    return;
  }
  g.schedPending  = true;
  g.schedLastSend = now;
  g.schedActive   = !release;
}

/* ------------------------------------------------------------------------ */
/* session                                                                   */

static bool sessionCancelled(void * opaque)
{
  (void)opaque;
  return !atomic_load(&g.running);
}

static void stopVideo(void)
{
  if (!atomic_load(&g.videoRunning))
    return;

  atomic_store(&g.videoRunning, false);
  const LG_FrameOps * ops = g.video->frame;
  ops->cancelFrameWait(g.transport);
  if (ops->cancelPointerWait)
    ops->cancelPointerWait(g.transport);
  pthread_cond_broadcast(&g.clCond);

  pthread_join(g.frameThread, NULL);
  if (ops->nextPointer)
    pthread_join(g.pointerThread, NULL);
  pthread_join(g.clipThread, NULL);

  atomic_store(&g.inListening, false);
  pthread_mutex_lock(&g.inLock);
  if (g.inOps)
  {
    g.inOps->reset(g.inOpaque);
    if (g.inOps->setStatusListener)
      g.inOps->setStatusListener(g.inOpaque, NULL, NULL);
  }
  g.inOps    = NULL;
  g.inOpaque = NULL;
  pthread_mutex_unlock(&g.inLock);
  pthread_mutex_lock(&g.stLock);
  memset(&g.inStatus, 0, sizeof(g.inStatus));
  pthread_mutex_unlock(&g.stLock);

  pthread_mutex_lock(&g.lock);
  if (g.clOps)
  {
    if (atomic_load(&g.clAttached))
      g.clOps->detach(g.clOpaque);
    if (g.clListening)
      g.clOps->setStatusListener(g.clOpaque, NULL, NULL);
  }
  g.clOps       = NULL;
  g.clOpaque    = NULL;
  g.clListening = false;
  atomic_store(&g.clAttached, false);
  atomic_store(&g.clStatusAvail, false);
  pthread_mutex_unlock(&g.lock);

  pthread_mutex_lock(&g.clLock);
  clipFreeOut();
  g.clGuestTypeCount = 0;
  pthread_mutex_unlock(&g.clLock);
}

static void startVideo(void)
{
  atomic_store(&g.lost, false);
  atomic_store(&g.videoRunning, true);

  pthread_mutex_lock(&g.inLock);
  void * opaque = NULL;
  const LG_InputOps * in = LGT_LGMP.getInputOps ?
    LGT_LGMP.getInputOps(g.transport, &opaque) : NULL;
  g.inOps    = in;
  g.inOpaque = opaque;
  pthread_mutex_unlock(&g.inLock);
  atomic_store(&g.inListening, true);
  if (in && in->setStatusListener)
    in->setStatusListener(opaque, inputStatusFn, NULL);

  opaque = NULL;
  const LG_ClipboardOps * cl = LGT_LGMP.getClipboardOps ?
    LGT_LGMP.getClipboardOps(g.transport, &opaque) : NULL;
  if (cl)
  {
    pthread_mutex_lock(&g.lock);
    g.clOps    = cl;
    g.clOpaque = opaque;
    atomic_store(&g.clAttached, false);
    atomic_store(&g.clStatusAvail, !cl->setStatusListener);
    g.clListening = cl->setStatusListener != NULL;
    pthread_mutex_unlock(&g.lock);
    /* reports the current status synchronously; attach happens from the
     * session loop once the guest clipboard is available */
    if (cl->setStatusListener)
      cl->setStatusListener(opaque, clipStatusFn, NULL);
    clipMaybeAttach();
  }

  pthread_create(&g.frameThread, NULL, frameThreadFn, NULL);
  if (g.video->frame->nextPointer)
    pthread_create(&g.pointerThread, NULL, pointerThreadFn, NULL);
  pthread_create(&g.clipThread, NULL, clipThreadFn, NULL);
}

static void fillSession(const LG_TransportSession * s)
{
  VGSessionInfo * o = &g.session;
  memset(o, 0, sizeof(*o));
  memcpy(o->version,  s->version,  sizeof(o->version)  - 1);
  memcpy(o->osName,   s->osName,   sizeof(o->osName)   - 1);
  memcpy(o->capture,  s->capture,  sizeof(o->capture)  - 1);
  memcpy(o->cpuModel, s->cpuModel, sizeof(o->cpuModel) - 1);
  o->features  = s->features;
  o->os        = s->os;
  o->cpus      = s->cpus;
  o->cores     = s->cores;
  o->sockets   = s->sockets;
  o->uuidValid = s->uuidValid;
  memcpy(o->uuid, s->uuid, 16);
  if (s->versionMismatch.valid)
  {
    o->expectedVersion = s->versionMismatch.expectedVersion;
    o->currentVersion  = s->versionMismatch.currentVersion;
    memcpy(o->mismatchComponent, s->versionMismatch.component,
        sizeof(o->mismatchComponent) - 1);
  }
}

static void setState(int s)
{
  if (atomic_exchange(&g.state, s) != s)
    vg_signal(VG_EVENT_STATE);
}

/* destroy the transport so the next iteration re-opens the shm device */
static void dropTransport(void)
{
  pthread_mutex_lock(&g.lock);
  LG_Transport * t = g.transport;
  g.transport = NULL;
  g.video     = NULL;
  pthread_mutex_unlock(&g.lock);
  if (t)
    LGT_LGMP.destroy(&t);
}

/* connect attempts before the shm device is re-opened (~5 seconds); a VM
 * restart may have re-created the shared memory file */
#define RECREATE_AFTER 20

static void * sessionThreadFn(void * unused)
{
  (void)unused;
  int failures = 0;
  while (atomic_load(&g.running))
  {
    /* (re)create the transport, this opens the shm device */
    if (!g.transport)
    {
      LG_Transport * t = NULL;
      if (!LGT_LGMP.create(&t))
      {
        setState(VG_STATE_NO_DEVICE);
        for (int i = 0; i < 10 && atomic_load(&g.running); ++i)
          vg_sleep_ms(100);
        continue;
      }
      failures = 0;
      pthread_mutex_lock(&g.lock);
      g.transport = t;
      g.video     = LGT_LGMP.getVideoOps(t);
      pthread_mutex_unlock(&g.lock);
    }

    LG_TransportSession session;
    memset(&session, 0, sizeof(session));
    LG_TransportStatus status = LGT_LGMP.connectCancellable(g.transport,
        &session, sessionCancelled, NULL);

    if (status != LG_TRANSPORT_OK)
    {
      if (status == LG_TRANSPORT_INVALID_VERSION)
      {
        pthread_mutex_lock(&g.lock);
        fillSession(&session);
        pthread_mutex_unlock(&g.lock);
        setState(VG_STATE_BAD_VERSION);
      }
      else
        setState(VG_STATE_WAITING);
      vg_sleep_ms(250);
      if (++failures >= RECREATE_AFTER)
      {
        failures = 0;
        dropTransport();
      }
      continue;
    }

    pthread_mutex_lock(&g.lock);
    fillSession(&session);
    pthread_mutex_unlock(&g.lock);

    startVideo();
    setState(VG_STATE_CONNECTED);

    g.schedActive  = false;
    g.schedPending = false;
    while (atomic_load(&g.running) && !atomic_load(&g.lost) &&
        LGT_LGMP.sessionValid(g.transport))
    {
      schedUpdate(false);
      clipMaybeAttach();
      vg_sleep_ms(20);
    }
    if (!atomic_load(&g.lost))
      schedUpdate(true);

    stopVideo();
    LGT_LGMP.disconnect(g.transport);
    setState(VG_STATE_WAITING);

    /* recreate the transport after a lost session so a restarted VM with a
     * re-created shm file is picked up */
    dropTransport();
    failures = 0;
    if (atomic_load(&g.running))
      vg_sleep_ms(250);
  }

  dropTransport();
  setState(VG_STATE_IDLE);
  return NULL;
}

/* ------------------------------------------------------------------------ */
/* public API                                                                */

VG_EXPORT int vg_version(void)
{
  return 1;
}

VG_EXPORT int vg_eventfd(void)
{
  return g.efd;
}

VG_EXPORT unsigned vg_take_events(void)
{
  if (g.efd >= 0)
  {
    uint64_t v;
    ssize_t r = read(g.efd, &v, sizeof(v));
    (void)r;
  }
  return atomic_exchange(&g.events, 0);
}

VG_EXPORT bool vg_start(const char * shmPath)
{
  if (atomic_load(&g.running))
    return false;

  if (!g.setup)
  {
    debug_init();
    pthread_mutex_init(&g.lock, NULL);
    pthread_mutex_init(&g.fbLock, NULL);
    pthread_mutex_init(&g.curLock, NULL);
    pthread_mutex_init(&g.inLock, NULL);
    pthread_mutex_init(&g.stLock, NULL);
    pthread_mutex_init(&g.clLock, NULL);
    pthread_cond_init(&g.clCond, NULL);
    g.efd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    g.fbWrite = 0; g.fbReady = 1; g.fbDisplay = 2;
    LGT_LGMP.setup();
    g.setup = true;
  }

  snprintf(g.shmPath, sizeof(g.shmPath), "%s", shmPath);
  option_set_string("lgmp", "shmDevice", g.shmPath);
  option_set_bool("lgmp", "allowDMA", false);

  pthread_mutex_lock(&g.fbLock);
  g.fbFresh = false;
  pthread_mutex_unlock(&g.fbLock);
  g.clCmdNotifyCount = -1;
  if (!atomic_load(&g.schedPeriodNs))
    atomic_store(&g.schedPeriodNs, 1000000000ULL / 60);
  atomic_store(&g.running, true);
  setState(VG_STATE_WAITING);
  if (pthread_create(&g.sessionThread, NULL, sessionThreadFn, NULL) != 0)
  {
    atomic_store(&g.running, false);
    return false;
  }
  return true;
}

VG_EXPORT void vg_stop(void)
{
  if (!atomic_load(&g.running))
    return;
  atomic_store(&g.running, false);
  pthread_join(g.sessionThread, NULL);
}

VG_EXPORT int vg_state(void)
{
  return atomic_load(&g.state);
}

VG_EXPORT bool vg_session(VGSessionInfo * out)
{
  pthread_mutex_lock(&g.lock);
  *out = g.session;
  pthread_mutex_unlock(&g.lock);
  return true;
}

VG_EXPORT uint64_t vg_frame_count(void)
{
  return atomic_load(&g.frameCount);
}

/* Swap in the newest frame for display. Returns true if `info`/`data` were
 * filled with a new frame. The display buffer stays valid until the next
 * call to vg_frame_acquire. */
VG_EXPORT bool vg_frame_acquire(VGFrameInfo * info, const uint8_t ** data)
{
  pthread_mutex_lock(&g.fbLock);
  if (!g.fbFresh)
  {
    pthread_mutex_unlock(&g.fbLock);
    return false;
  }
  const int tmp = g.fbDisplay;
  g.fbDisplay = g.fbReady;
  g.fbReady   = tmp;
  g.fbFresh   = false;
  pthread_mutex_unlock(&g.fbLock);

  *info = g.fb[g.fbDisplay].info;
  *data = g.fb[g.fbDisplay].data;
  return true;
}

/* the last displayed frame, for screenshots */
VG_EXPORT bool vg_frame_current(VGFrameInfo * info, const uint8_t ** data)
{
  if (!g.fb[g.fbDisplay].data || !g.fb[g.fbDisplay].info.size)
    return false;
  *info = g.fb[g.fbDisplay].info;
  *data = g.fb[g.fbDisplay].data;
  return true;
}

/* Copy cursor state. If `shape` is non-null and large enough the BGRA image
 * is copied as well. */
VG_EXPORT bool vg_cursor_get(VGCursorInfo * info, uint8_t * shape,
    size_t shapeSize)
{
  pthread_mutex_lock(&g.curLock);
  *info = g.cursor;
  const size_t need = (size_t)g.cursor.width * g.cursor.height * 4;
  if (shape && need && shapeSize >= need && g.curData)
    memcpy(shape, g.curData, need);
  pthread_mutex_unlock(&g.curLock);
  return true;
}

/* input --------------------------------------------------------------------*/

#define WITH_INPUT(expr) \
  bool ret = false; \
  pthread_mutex_lock(&g.inLock); \
  if (g.inOps) ret = (expr); \
  pthread_mutex_unlock(&g.inLock); \
  return ret;

VG_EXPORT bool vg_input_available(void)
{
  pthread_mutex_lock(&g.inLock);
  const bool ops = g.inOps != NULL;
  pthread_mutex_unlock(&g.inLock);
  pthread_mutex_lock(&g.stLock);
  const bool r = ops && g.inStatus.available;
  pthread_mutex_unlock(&g.stLock);
  return r;
}

VG_EXPORT int vg_input_leds(void)
{
  pthread_mutex_lock(&g.inLock);
  const bool ops = g.inOps != NULL;
  pthread_mutex_unlock(&g.inLock);
  pthread_mutex_lock(&g.stLock);
  const int r = ops && g.inStatus.keyboardLEDsValid ?
    g.inStatus.keyboardLEDs : -1;
  pthread_mutex_unlock(&g.stLock);
  return r;
}

VG_EXPORT bool vg_input_abs_supported(void)
{
  WITH_INPUT(g.inOps->supports(g.inOpaque, LG_INPUT_SUPPORT_MOUSE_ABSOLUTE))
}

VG_EXPORT bool vg_key(int linuxKey, bool down)
{
  WITH_INPUT(down ? g.inOps->keyDown(g.inOpaque, linuxKey) :
                    g.inOps->keyUp  (g.inOpaque, linuxKey))
}

VG_EXPORT bool vg_mouse_position(uint32_t x, uint32_t y, uint32_t w, uint32_t h)
{
  WITH_INPUT(g.inOps->mousePosition(g.inOpaque, x, y, w, h))
}

VG_EXPORT bool vg_mouse_motion(int32_t dx, int32_t dy)
{
  WITH_INPUT(g.inOps->mouseMotion(g.inOpaque, dx, dy))
}

VG_EXPORT bool vg_mouse_button(unsigned button, bool down)
{
  WITH_INPUT(down ? g.inOps->mousePress  (g.inOpaque, button) :
                    g.inOps->mouseRelease(g.inOpaque, button))
}

VG_EXPORT bool vg_keyboard_leds(bool num, bool caps, bool scroll)
{
  WITH_INPUT(g.inOps->keyboardLEDs ?
      g.inOps->keyboardLEDs(g.inOpaque, num, caps, scroll) : false)
}

VG_EXPORT void vg_input_reset(void)
{
  pthread_mutex_lock(&g.inLock);
  if (g.inOps)
    g.inOps->reset(g.inOpaque);
  pthread_mutex_unlock(&g.inLock);
}

/* control ------------------------------------------------------------------*/

/* set the presentation refresh rate used for frame scheduling */
VG_EXPORT void vg_set_refresh(double hz)
{
  if (hz < 1.0)
    hz = 1.0;
  if (hz > 500.0)
    hz = 500.0;
  atomic_store(&g.schedPeriodNs, (uint64_t)(1e9 / hz));
  atomic_store(&g.schedImmediate, true);
}

/* ask the host to deliver a fresh frame right away */
VG_EXPORT void vg_request_frame(void)
{
  atomic_store(&g.schedImmediate, true);
}

VG_EXPORT int vg_window_size(uint32_t w, uint32_t h)
{
  int ret = -1;
  pthread_mutex_lock(&g.lock);
  if (g.transport && atomic_load(&g.state) == VG_STATE_CONNECTED &&
      (g.session.features & LG_TRANSPORT_FEATURE_WINDOW_SIZE))
  {
    LG_TransportControl c =
    {
      .type       = LG_TRANSPORT_CONTROL_WINDOW_SIZE,
      .windowSize = { .width = w, .height = h },
    };
    LG_TransportControlToken token;
    ret = LGT_LGMP.sendControl(g.transport, &c, &token);
  }
  pthread_mutex_unlock(&g.lock);
  return ret;
}

VG_EXPORT int vg_set_cursor_pos(int32_t x, int32_t y)
{
  int ret = -1;
  pthread_mutex_lock(&g.lock);
  if (g.transport && atomic_load(&g.state) == VG_STATE_CONNECTED &&
      (g.session.features & LG_TRANSPORT_FEATURE_SET_CURSOR_POS))
  {
    LG_TransportControl c =
    {
      .type      = LG_TRANSPORT_CONTROL_SET_CURSOR_POS,
      .cursorPos = { .x = x, .y = y },
    };
    LG_TransportControlToken token;
    ret = LGT_LGMP.sendControl(g.transport, &c, &token);
  }
  pthread_mutex_unlock(&g.lock);
  return ret;
}

/* clipboard ----------------------------------------------------------------*/

VG_EXPORT bool vg_clip_available(void)
{
  pthread_mutex_lock(&g.lock);
  const bool r = g.clOps && atomic_load(&g.clAttached) &&
    atomic_load(&g.clStatusAvail);
  pthread_mutex_unlock(&g.lock);
  return r;
}

/* guest offered types; returns count and the serial of the offer */
VG_EXPORT int vg_clip_guest_types(int * types, int max, uint64_t * serial)
{
  pthread_mutex_lock(&g.clLock);
  int n = g.clGuestTypeCount < max ? g.clGuestTypeCount : max;
  for (int i = 0; i < n; ++i)
    types[i] = g.clGuestTypes[i];
  *serial = g.clGuestSerial;
  pthread_mutex_unlock(&g.clLock);
  return n;
}

/* ask the guest for its clipboard contents in the given type */
VG_EXPORT void vg_clip_request(int type)
{
  pthread_mutex_lock(&g.clLock);
  g.clCmdRequest     = true;
  g.clCmdRequestType = type;
  pthread_cond_signal(&g.clCond);
  pthread_mutex_unlock(&g.clLock);
}

/* fetch completed incoming data. Returns the size, or -1 if none. If buf is
 * NULL only the size/serial are returned. */
/* Fetch completed incoming data. Returns -1 if there is none, otherwise the
 * size of the data. The data, type and serial are only filled in (and the
 * data consumed) when buf is large enough; otherwise call again with a buffer
 * of at least the returned size. */
VG_EXPORT int64_t vg_clip_take(uint8_t * buf, uint64_t bufSize, int * type,
    uint64_t * serial)
{
  int64_t r = -1;
  pthread_mutex_lock(&g.clLock);
  if (g.clInDone)
  {
    r = g.clInSize;
    if (buf && bufSize >= g.clInSize)
    {
      memcpy(buf, g.clInData, g.clInSize);
      *type      = g.clInType;
      *serial    = g.clInSerial;
      g.clInDone = false;
    }
  }
  pthread_mutex_unlock(&g.clLock);
  return r;
}

/* announce that the host clipboard has these types (count 0 = release) */
VG_EXPORT void vg_clip_notify(const int * types, int count)
{
  pthread_mutex_lock(&g.clLock);
  if (count > CLIP_MAX_TYPES)
    count = CLIP_MAX_TYPES;
  for (int i = 0; i < count; ++i)
    g.clCmdNotify[i] = types[i];
  g.clCmdNotifyCount = count;
  pthread_cond_signal(&g.clCond);
  pthread_mutex_unlock(&g.clLock);
}

/* pending guest request for host data; returns the type or -1 */
VG_EXPORT int vg_clip_guest_request(uint64_t * serial)
{
  pthread_mutex_lock(&g.clLock);
  const int r = g.clGuestRequest != LG_CLIPBOARD_REQUEST_INVALID ?
    (int)g.clGuestRequestType : -1;
  *serial = g.clGuestRequestSerial;
  pthread_mutex_unlock(&g.clLock);
  return r;
}

/* refuse the pending guest request (we have nothing to give) so the guest's
 * request slot is freed */
VG_EXPORT void vg_clip_cancel(void)
{
  pthread_mutex_lock(&g.clLock);
  if (g.clGuestRequest != LG_CLIPBOARD_REQUEST_INVALID)
  {
    g.clCmdCancel    = g.clGuestRequest;
    g.clGuestRequest = LG_CLIPBOARD_REQUEST_INVALID;
    pthread_cond_signal(&g.clCond);
  }
  pthread_mutex_unlock(&g.clLock);
}

/* answer the pending guest request */
VG_EXPORT bool vg_clip_reply(int type, const uint8_t * data, uint64_t size)
{
  pthread_mutex_lock(&g.clLock);
  if (g.clGuestRequest == LG_CLIPBOARD_REQUEST_INVALID)
  {
    pthread_mutex_unlock(&g.clLock);
    return false;
  }
  clipFreeOut();
  g.clOut.data = malloc(size ? size : 1);
  if (!g.clOut.data)
  {
    pthread_mutex_unlock(&g.clLock);
    return false;
  }
  memcpy(g.clOut.data, data, size);
  g.clOut.size    = size;
  g.clOut.type    = type;
  g.clOut.request = g.clGuestRequest;
  g.clOut.active  = true;
  g.clOut.stage   = 0;
  g.clGuestRequest = LG_CLIPBOARD_REQUEST_INVALID;
  pthread_cond_signal(&g.clCond);
  pthread_mutex_unlock(&g.clLock);
  return true;
}
