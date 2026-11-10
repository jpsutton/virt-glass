# Builds libvglass.so: the vendored Looking Glass LGMP transport plus the
# virt-glass shim (vglass.c).

LG      := third_party/lg
OUT     ?= ../virtglass/native
TARGET  := $(OUT)/libvglass.so

CFLAGS  ?= -O2 -g
CFLAGS  += -std=gnu11 -fPIC -march=x86-64-v2 -D_GNU_SOURCE \
           -fvisibility=hidden -Wall -Wno-unused-function -Wno-deprecated-declarations \
           -I$(LG) -I$(LG)/common/include -I$(LG)/client/include \
           -I$(LG)/client/src -I$(LG)/client/transports/LGMP \
           -I$(LG)/repos/LGMP/lgmp/include -I$(LG)/repos/LGProtocol/include
LDFLAGS += -shared -Wl,--no-undefined
LDLIBS  += -lpthread -lm

LG_SRCS := \
  $(LG)/client/transports/LGMP/lgmp.c \
  $(LG)/client/transports/LGMP/input.c \
  $(LG)/client/transports/LGMP/clipboard.c \
  $(LG)/client/src/kb_hid.c \
  $(LG)/repos/LGMP/lgmp/src/client.c \
  $(LG)/repos/LGMP/lgmp/src/host.c \
  $(LG)/repos/LGMP/lgmp/src/status.c \
  $(LG)/repos/LGMP/lgmp/src/stream.c \
  $(LG)/repos/LGMP/lgmp/src/spmc.c \
  $(LG)/common/src/framebuffer.c \
  $(LG)/common/src/option.c \
  $(LG)/common/src/stringutils.c \
  $(LG)/common/src/stringlist.c \
  $(LG)/common/src/debug.c \
  $(LG)/common/src/cpuinfo.c \
  $(LG)/common/src/KVMFR.c \
  $(LG)/common/src/ll.c \
  $(LG)/common/src/vector.c \
  $(LG)/common/src/appstrings.c \
  $(LG)/common/src/platform/linux/event.c \
  $(LG)/common/src/platform/linux/ivshmem.c \
  $(LG)/common/src/platform/linux/thread.c \
  $(LG)/common/src/platform/linux/time.c \
  $(LG)/common/src/platform/linux/debug.c \
  $(LG)/common/src/platform/linux/cpuinfo.c \
  $(LG)/common/src/platform/linux/sysinfo.c

SRCS := vglass.c $(LG_SRCS)
BUILD := build
OBJS := $(patsubst %.c,$(BUILD)/%.o,$(SRCS))

all: $(TARGET)

$(TARGET): $(OBJS)
	@mkdir -p $(OUT)
	$(CC) $(LDFLAGS) -o $@ $^ $(LDLIBS)

# upstream sources are compiled quietly; only our shim gets full warnings
$(BUILD)/$(LG)/%.o: $(LG)/%.c
	@mkdir -p $(dir $@)
	@$(CC) $(CFLAGS) -w -c -o $@ $<

$(BUILD)/%.o: %.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -rf $(BUILD) $(TARGET)

.PHONY: all clean
