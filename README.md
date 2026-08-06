Claude-maxxed prototype to demonstrate an alternate development paradigm for spack.

Rather than using the entire spack toolchain to build development portions of a software stack, instead this works by taking an installed/concrete spec as
frozen and attempting to cut/splice/shadow portions of the graph. The goal is to avoid concretization/solving a new graph + building unnecessary portions of the software DAG.
Users choose portions of the graph they would like to develop. They can add on new packages or change dependencies of developing packages,
and checks are made against the static graph (a new graph is not re-solved).

It can also be used to create relocatable software by using RUNPATH (which is superseded by LD_LIBRARY_PATH rather than RPATH).

There is a command to create tarballs that can be published to cvmfs along with a setup script that defines the environment. This isn't perfect but is a demonstration of what we can do with better coding of this.

Caveat: since it relies on a spack env's externals, one must be able to provide these. I.e. on a worker node image missing make/ninja, one must bind mount that into the container.



## Steps ###
