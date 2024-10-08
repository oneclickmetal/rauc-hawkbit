#!/usr/bin/env python
# -*- coding: utf-8 -*-

from setuptools import setup, find_packages

setup(
      name='rauc-hawkbit',
      description='hawkBit client for RAUC',
      author='Bastian Stender and Enrico Joerns',
      author_email='entwicklung@pengutronix.de',
      license='LGPL-2.1',
      use_scm_version=True,
      url='https://github.com/rauc/rauc-hawkbit',
      setup_requires=['setuptools_scm'],
      install_requires=[
          'aiohttp==3.9.1',
          'asyncio-glib==0.1',
          'PyGObject==3.44.2'
      ],
      packages=find_packages(),
      include_package_data=True,
      zip_safe=False,
      scripts=[
          'bin/rauc-hawkbit-client'
      ]
)
