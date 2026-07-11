# Point in Polygon Service Review

## [PR 1 & 2](https://github.com/martin-gleason/point-in-polygon-service/pull/2)

## Project Description
Porting old Arcpy: Point in Polygon into a new service that is easy to read and open source. 

## Feature Description - F1 and F2
Setting up config and fast fail processes, and a simple web form to test functions (ahead of f3 and f4)

## What I learned
- the intial code base set up a lot of the work, but was tied to arcpy (on purpose)
- This edition is an attempt to move beyond one tool set
- this includes updating pip, uv, and going from flask to fastapi
- adding the web form lets me test more so i can see how things work

## What to do different
- Why TOML instead of YAML?
  - [TOML is minimal, human readable configuration](https://toml.io/en/)
  - perfect for configuration
  - why not json?
    - i bet its just less complicated