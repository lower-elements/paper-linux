ESPEAK_NG_VERSION = 1.52.0
ESPEAK_NG_SOURCE = $(ESPEAK_NG_VERSION).tar.gz
ESPEAK_NG_SITE = https://github.com/espeak-ng/espeak-ng/archive/refs/tags
ESPEAK_NG_LICENSE = GPL-3.0+, Apache-2.0, BSD-2-Clause, Unicode-DFS-2016
ESPEAK_NG_LICENSE_FILES = COPYING COPYING.APACHE COPYING.BSD2 COPYING.UCD
ESPEAK_NG_AUTORECONF = YES
ESPEAK_NG_AUTORECONF_ENV = \
	AUTOMAKE="$(HOST_DIR)/bin/automake --foreign"
define ESPEAK_NG_TOUCH_AUTOMAKE_FILES
	touch $(@D)/AUTHORS $(@D)/NEWS
endef
ESPEAK_NG_POST_EXTRACT_HOOKS += ESPEAK_NG_TOUCH_AUTOMAKE_FILES

ESPEAK_NG_DEPENDENCIES = host-espeak-ng
ESPEAK_NG_MAKE = $(MAKE1)
ESPEAK_NG_CONF_ENV = \
	RONN=no \
	KRAMDOWN=no
ESPEAK_NG_CONF_OPTS = \
	--enable-rpath=no
ifeq ($(BR2_PACKAGE_PCAUDIOLIB),y)
ESPEAK_NG_DEPENDENCIES += pcaudiolib
ESPEAK_NG_CONF_OPTS += --with-pcaudiolib=yes
else
ESPEAK_NG_CONF_OPTS += --with-pcaudiolib=no
endif

ifeq ($(BR2_PACKAGE_LIBSONIC),y)
ESPEAK_NG_DEPENDENCIES += libsonic
ESPEAK_NG_CONF_OPTS += --with-sonic=yes
else
ESPEAK_NG_CONF_OPTS += --with-sonic=no
endif

ifeq ($(BR2_PACKAGE_ESPEAK_NG_SPEECH_PLAYER),y)
ESPEAK_NG_CONF_OPTS += --with-speechplayer=yes
else
ESPEAK_NG_CONF_OPTS += --with-speechplayer=no
endif

ifeq ($(BR2_PACKAGE_ESPEAK_NG_KLATT),y)
ESPEAK_NG_CONF_OPTS += --with-klatt=yes
else
ESPEAK_NG_CONF_OPTS += --with-klatt=no
endif

ifeq ($(BR2_PACKAGE_ESPEAK_NG_ASYNC),y)
ESPEAK_NG_CONF_OPTS += --with-async=yes
else
ESPEAK_NG_CONF_OPTS += --with-async=no
endif

ifeq ($(BR2_PACKAGE_ESPEAK_NG_EXTDICT_RU),y)
ESPEAK_NG_CONF_OPTS += --with-extdict-ru=yes
else
ESPEAK_NG_CONF_OPTS += --with-extdict-ru=no
endif

ifeq ($(BR2_PACKAGE_ESPEAK_NG_EXTDICT_CMN),y)
ESPEAK_NG_CONF_OPTS += --with-extdict-cmn=yes
else
ESPEAK_NG_CONF_OPTS += --with-extdict-cmn=no
endif

ifeq ($(BR2_PACKAGE_ESPEAK_NG_EXTDICT_YUE),y)
ESPEAK_NG_CONF_OPTS += --with-extdict-yue=yes
else
ESPEAK_NG_CONF_OPTS += --with-extdict-yue=no
endif

ifeq ($(BR2_PACKAGE_ESPEAK_NG_MBROLA),y)
ESPEAK_NG_CONF_OPTS += --with-mbrola=yes
else
ESPEAK_NG_CONF_OPTS += --with-mbrola=no
endif
ESPEAK_NG_MAKE_OPTS = \
	ESPEAK_NG_DATA_COMPILER="$(HOST_DIR)/bin/espeak-ng" \
	ESPEAK_NG_DATA_COMPILER_LD_LIBRARY_PATH="$(HOST_DIR)/lib" \
	src_speak_ng_LDFLAGS="-lm"
ESPEAK_NG_INSTALL_TARGET_OPTS = $(ESPEAK_NG_MAKE_OPTS)

HOST_ESPEAK_NG_AUTORECONF = YES
HOST_ESPEAK_NG_AUTORECONF_ENV = \
	AUTOMAKE="$(HOST_DIR)/bin/automake --foreign"
HOST_ESPEAK_NG_POST_EXTRACT_HOOKS += ESPEAK_NG_TOUCH_AUTOMAKE_FILES
HOST_ESPEAK_NG_MAKE = $(MAKE1)
HOST_ESPEAK_NG_CONF_ENV = \
	RONN=no \
	KRAMDOWN=no
HOST_ESPEAK_NG_CONF_OPTS = \
	--enable-rpath=no \
	--with-pcaudiolib=no \
	--with-sonic=no \
	--with-speechplayer=no
HOST_ESPEAK_NG_MAKE_OPTS = \
	src_speak_ng_LDFLAGS="-lm"

ifeq ($(BR2_PACKAGE_ESPEAK_NG_MBROLA),y)
HOST_ESPEAK_NG_CONF_OPTS += --with-mbrola=yes
else
HOST_ESPEAK_NG_CONF_OPTS += --with-mbrola=no
endif

$(eval $(autotools-package))
$(eval $(host-autotools-package))
