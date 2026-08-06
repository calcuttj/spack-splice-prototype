Claude-maxxed prototype to demonstrate an alternate development paradigm for spack.

Rather than using the entire spack toolchain to build development portions of a software stack, instead this works by taking an installed/concrete spec as
frozen and attempting to cut/splice/shadow portions of the graph. The goal is to avoid concretization/solving a new graph + building unnecessary portions of the software DAG.
Users choose portions of the graph they would like to develop. They can add on new packages or change dependencies of developing packages,
and checks are made against the static graph (a new graph is not re-solved).

It can also be used to create relocatable software by using RUNPATH (which is superseded by LD_LIBRARY_PATH rather than RPATH).

There is a command to create tarballs that can be published to cvmfs along with a setup script that defines the environment. This isn't perfect but is a demonstration of what we can do with better coding of this.

Caveat: since it might rely on a spack env's externals, one must be able to provide these when building. I.e. on a worker node image missing make/ninja, one must bind mount that into the container.



## Steps ###
### -1. Set up a subspack ###
Follow the instructions [here]([url](https://fnalssi.github.io/spack-at-fnal/pages/build_manager_process.html#preparing-a-spack-build-instance)) 


This allows you to see several environments with specs you can build off of

i.e. by runing 
`spack -e dunesw-10_22_00d00-justin-01_06_01-prototype spec`

### 0. Set up spack-splice ###
<pre>
git clone git@github.com:calcuttj/spack-splice-prototype.git
spack config add $PWD/spack-splice-prototype/spack-splice
</pre>

### 1. Set up dev area 
<pre>
  spack splice init dunesw-10_22_00d00-justin-01_06_01-prototype --root dunesw
</pre>

### 2. Add a package to develop 
<pre>
  spack splice add cetlib-except
</pre>

### 3. Fetch its src
<pre>
  spack splice src 
</pre>

### 4. Build it
<pre>
  spack splice build
</pre>

### 5. Optional: tar it
<pre>
  spack splice pack 
</pre>
