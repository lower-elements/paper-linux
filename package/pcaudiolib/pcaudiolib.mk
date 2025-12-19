PCAUDIOLIB_VERSION = 1.3
PCAUDIOLIB_SOURCE = $(PCAUDIOLIB_VERSION).tar.gz
PCAUDIOLIB_SITE = https://github.com/espeak-ng/pcaudiolib/archive/refs/tags
PCAUDIOLIB_LICENSE = GPL-3.0+
PCAUDIOLIB_LICENSE_FILES = COPYING

PCAUDIOLIB_DEPENDENCIES = alsa-lib
PCAUDIOLIB_INSTALL_STAGING = YES
PCAUDIOLIB_AUTORECONF = YES
define PCAUDIOLIB_TOUCH_AUTOMAKE_FILES
	touch $(@D)/NEWS
endef
PCAUDIOLIB_POST_EXTRACT_HOOKS += PCAUDIOLIB_TOUCH_AUTOMAKE_FILES
PCAUDIOLIB_CONF_OPTS = \
	--with-alsa=yes \
	--with-pulseaudio=no \
	--with-oss=no \
	--with-qsa=no \
	--with-coreaudio=no

$(eval $(autotools-package))
