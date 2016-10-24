import logging
import shutil
import sys

import tabulate
import thespian.actors
from esrally import config, exceptions, paths, track, driver, reporter, metrics, time, PROGRAM_NAME
from esrally.mechanic import mechanic
from esrally.utils import console, io, convert

logger = logging.getLogger("rally.racecontrol")

pipelines = {}


class Pipeline:
    """
    Describes a whole execution pipeline. A pipeline can consist of one or more steps. Each pipeline should contain roughly of the following
    steps:

    * Prepare the benchmark candidate: It can build Elasticsearch from sources, download a ZIP from somewhere etc.
    * Launch the benchmark candidate: This can be done directly, with tools like Ansible or it can assume the candidate is already launched
    * Run the benchmark
    * Report results
    """

    def __init__(self, name, description, target, stable=True):
        """
        Creates a new pipeline.

        :param name: A short name of the pipeline. This name will be used to reference it from the command line.
        :param description: A human-readable description what the pipeline does.
        :param target: A function that implements this pipeline
        :param stable True iff the pipeline is considered production quality.
        """
        self.name = name
        self.description = description
        self.target = target
        self.stable = stable
        pipelines[name] = self

    def __call__(self, cfg):
        self.target(cfg)


def sweep(cfg):
    invocation_root = cfg.opts("system", "invocation.root.dir")
    track_name = cfg.opts("benchmarks", "track")
    challenge_name = cfg.opts("benchmarks", "challenge")
    car_name = cfg.opts("benchmarks", "car")

    log_root = paths.Paths(cfg).log_root()
    archive_path = "%s/logs-%s-%s-%s.zip" % (invocation_root, track_name, challenge_name, car_name)
    io.compress(log_root, archive_path)
    console.println("")
    console.info("Archiving logs in %s" % archive_path)
    shutil.rmtree(log_root)


def benchmark(cfg, mechanic, metrics_store):
    track_name = cfg.opts("benchmarks", "track")
    challenge_name = cfg.opts("benchmarks", "challenge")
    selected_car_name = cfg.opts("benchmarks", "car")
    rounds = cfg.opts("benchmarks", "rounds")

    console.info("Racing on track [%s], challenge [%s] and car [%s]" % (track_name, challenge_name, selected_car_name))

    mechanic.prepare_candidate()
    cluster = mechanic.start_engine()

    t = track.load_track(cfg)
    metrics.race_store(cfg).store_race(t)

    actors = thespian.actors.ActorSystem()
    # just ensure it is optically separated
    console.println("")
    lap_timer = time.Clock.stop_watch()
    lap_timer.start()
    lap_times = 0
    for round in range(0, rounds):
        if rounds > 1:
            msg = "Round [%d/%d]" % (round + 1, rounds)
            console.println(console.format.bold(msg), logger=logger.info)
            console.println(console.format.underline_for(msg))
        main_driver = actors.createActor(driver.Driver)
        cluster.on_benchmark_start()
        result = actors.ask(main_driver, driver.StartBenchmark(cfg, t, metrics_store.meta_info))
        if isinstance(result, driver.BenchmarkComplete):
            cluster.on_benchmark_stop()
            metrics_store.bulk_add(result.metrics)
        elif isinstance(result, driver.BenchmarkFailure):
            raise exceptions.RallyError(result.message, result.cause)
        else:
            raise exceptions.RallyError("Driver has returned no metrics but instead [%s]. Terminating race without result." % str(result))
        if rounds > 1:
            lap_time = lap_timer.split_time() - lap_times
            lap_times += lap_time
            hl, ml, sl = convert.seconds_to_hour_minute_seconds(lap_time)
            console.println("")
            if round + 1 < rounds:
                remaining = (rounds - round - 1) * lap_times / (round + 1)
                hr, mr, sr = convert.seconds_to_hour_minute_seconds(remaining)
                console.info("Lap time %02d:%02d:%02d (ETA: %02d:%02d:%02d)" % (hl, ml, sl, hr, mr, sr), logger=logger)
            else:
                console.info("Lap time %02d:%02d:%02d" % (hl, ml, sl), logger=logger)
            console.println("")

    mechanic.stop_engine(cluster)
    metrics_store.close()
    reporter.summarize(cfg, t)
    sweep(cfg)


# Poor man's curry
def from_sources_complete(cfg):
    metrics_store = metrics.metrics_store(cfg, read_only=False)
    return benchmark(cfg, mechanic.create(cfg, metrics_store, sources=True, build=True), metrics_store)


def from_sources_skip_build(cfg):
    metrics_store = metrics.metrics_store(cfg, read_only=False)
    return benchmark(cfg, mechanic.create(cfg, metrics_store, sources=True, build=False), metrics_store)


def from_distribution(cfg):
    metrics_store = metrics.metrics_store(cfg, read_only=False)
    return benchmark(cfg, mechanic.create(cfg, metrics_store, distribution=True), metrics_store)


def benchmark_only(cfg):
    # We'll use a special car name for external benchmarks.
    cfg.add(config.Scope.benchmark, "benchmarks", "car", "external")
    metrics_store = metrics.metrics_store(cfg, read_only=False)
    return benchmark(cfg, mechanic.create(cfg, metrics_store, external=True), metrics_store)


def docker(cfg):
    metrics_store = metrics.metrics_store(cfg, read_only=False)
    return benchmark(cfg, mechanic.create(cfg, metrics_store, docker=True), metrics_store)


Pipeline("from-sources-complete",
         "Builds and provisions Elasticsearch, runs a benchmark and reports results.", from_sources_complete)

Pipeline("from-sources-skip-build",
         "Provisions Elasticsearch (skips the build), runs a benchmark and reports results.", from_sources_skip_build)

Pipeline("from-distribution",
         "Downloads an Elasticsearch distribution, provisions it, runs a benchmark and reports results.", from_distribution)

Pipeline("benchmark-only",
         "Assumes an already running Elasticsearch instance, runs a benchmark and reports results", benchmark_only)

# Very experimental Docker pipeline. Should only be used with great care and is also not supported on all platforms.
Pipeline("docker",
         "Runs a benchmark against the official Elasticsearch Docker container and reports results", docker, stable=False)


def list_pipelines():
    console.println("Available pipelines:\n")
    console.println(tabulate.tabulate([[pipeline.name, pipeline.description] for pipeline in pipelines.values() if pipeline.stable],
                    headers=["Name", "Description"]))


def run(cfg):
    name = cfg.opts("system", "pipeline")
    try:
        pipeline = pipelines[name]
    except KeyError:
        raise exceptions.SystemSetupError(
            "Unknown pipeline [%s]. List the available pipelines with %s list pipelines." % (name, PROGRAM_NAME))
    try:
        pipeline(cfg)
    except exceptions.RallyError as e:
        # just pass on our own errors. It should be treated differently on top-level
        raise e
    except BaseException:
        tb = sys.exc_info()[2]
        raise exceptions.RallyError("This race ended with a fatal crash.").with_traceback(tb)
