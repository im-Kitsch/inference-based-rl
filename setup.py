from setuptools import find_packages
from setuptools import setup

install_requires = [
    'torch>=1.5.1',
    'gym==0.23.1',
    'numpy>=1.10.4',
    'pillow',
    "mujoco-py>=2.1, <2.2",
    "scipy",
    'hydra-core',
    # "hydra-core==1.0.6",
    # "omegaconf==2.0.6",
    "Cython<3"
]

setup(
    name='pfrlx',
    version='0.0.1',
    description='',
    author='Hiroki Furuta',
    author_email='',
    url='',
    license='MIT License',
    packages=find_packages(),
    install_requires=install_requires,
)
