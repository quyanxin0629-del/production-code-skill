// Template: validate_java.py inserts the actual Markdown examples before compiling.
import java.io.IOException;
import java.lang.reflect.Field;
import java.net.*;
import java.net.http.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import java.util.concurrent.locks.ReentrantLock;
import java.util.stream.IntStream;
import javax.net.ssl.*;

public class MechanismTests {
    /*__STRIPES__*/

    static final class Ledger {
        private final LocalStripes stripes = new LocalStripes();
        private final ConcurrentHashMap<String, Long> balances;
        Ledger(Map<String, Long> initial) { balances = new ConcurrentHashMap<>(initial); }
        /*__TRANSFER__*/
    }

    static final class SnapshotModel {
        /*__SNAPSHOT__*/
    }

    static final class Probe {
        /*__PROBE__*/
    }

    static void check(boolean value, String reason) {
        if (!value) throw new AssertionError(reason);
    }

    static void await(CountDownLatch latch) throws InterruptedException {
        check(latch.await(5, TimeUnit.SECONDS), "latch timed out");
    }

    static ReentrantLock[] locks(LocalStripes stripes) throws ReflectiveOperationException {
        Field field = LocalStripes.class.getDeclaredField("locks");
        field.setAccessible(true);
        return (ReentrantLock[]) field.get(stripes);
    }

    static void lockOrderingAndCleanup() throws Exception {
        LocalStripes stripes = new LocalStripes();
        ReentrantLock[] locks = locks(stripes);
        stripes.withKeys(List.of("Aa", "BB"), Duration.ofSeconds(1), () ->
                check(locks[stripes.index("Aa")].getHoldCount() == 1, "colliding stripe acquired twice"));
        try {
            stripes.withKeys(List.of("a", "b"), Duration.ofSeconds(1), () -> {
                throw new IllegalStateException("expected update failure");
            });
            throw new AssertionError("update exception lost");
        } catch (IllegalStateException expected) {
            check(Arrays.stream(locks).noneMatch(ReentrantLock::isLocked), "update failure leaked locks");
        }
        LocalStripes budgeted = new LocalStripes();
        ReentrantLock[] budgetLocks = locks(budgeted);
        int first = Math.min(budgeted.index("a"), budgeted.index("b"));
        int second = Math.max(budgeted.index("a"), budgeted.index("b"));
        AtomicLong firstBudget = new AtomicLong();
        AtomicLong secondBudget = new AtomicLong();
        budgetLocks[first] = new ReentrantLock() {
            @Override public boolean tryLock(long timeout, TimeUnit unit) throws InterruptedException {
                firstBudget.set(unit.toNanos(timeout));
                // Deliberately consume acquisition time; the next lock must receive less.
                new CountDownLatch(1).await(1, TimeUnit.MILLISECONDS);
                return super.tryLock(timeout, unit);
            }
        };
        budgetLocks[second] = new ReentrantLock() {
            @Override public boolean tryLock(long timeout, TimeUnit unit) throws InterruptedException {
                secondBudget.set(unit.toNanos(timeout));
                return super.tryLock(timeout, unit);
            }
        };
        budgeted.withKeys(List.of("b", "a"), Duration.ofSeconds(5), () -> {});
        check(secondBudget.get() > 0 && secondBudget.get() < firstBudget.get(),
                "each lock received a fresh budget");
        ExecutorService pool = Executors.newFixedThreadPool(2);
        CyclicBarrier start = new CyclicBarrier(3);
        int[] guarded = {0};
        try {
            List<Future<?>> runs = new ArrayList<>();
            for (List<String> order : List.of(List.of("a", "b"), List.of("b", "a"))) {
                runs.add(pool.submit(() -> {
                    start.await(5, TimeUnit.SECONDS);
                    for (int i = 0; i < 500; i++)
                        stripes.withKeys(order, Duration.ofSeconds(5), () -> guarded[0]++);
                    return null;
                }));
            }
            start.await(5, TimeUnit.SECONDS);
            for (Future<?> run : runs) run.get(5, TimeUnit.SECONDS);
            check(guarded[0] == 1000, "opposite-key updates lost state");
        } finally {
            pool.shutdownNow();
            check(pool.awaitTermination(5, TimeUnit.SECONDS), "lock test pool did not stop");
        }
    }

    static void partialAcquireExit(boolean interrupt) throws Exception {
        LocalStripes stripes = new LocalStripes();
        ReentrantLock[] locks = locks(stripes);
        int first = Math.min(stripes.index("a"), stripes.index("b"));
        int second = Math.max(stripes.index("a"), stripes.index("b"));
        CountDownLatch attemptingSecond = new CountDownLatch(1);
        locks[second] = new ReentrantLock() {
            @Override public boolean tryLock(long timeout, TimeUnit unit) throws InterruptedException {
                attemptingSecond.countDown();
                return super.tryLock(timeout, unit);
            }
        };
        CountDownLatch exited = new CountDownLatch(1);
        AtomicReference<Throwable> failure = new AtomicReference<>();
        Thread waiter = new Thread(() -> {
            try {
                stripes.withKeys(List.of("b", "a"),
                        interrupt ? Duration.ofSeconds(5) : Duration.ofMillis(50),
                        () -> { throw new AssertionError("update ran without the second lock"); });
                failure.set(new AssertionError("contended call unexpectedly succeeded"));
            } catch (Throwable caught) {
                failure.set(caught);
            } finally {
                exited.countDown();
            }
        });
        locks[second].lock();
        try {
            waiter.start();
            await(attemptingSecond);
            if (interrupt) waiter.interrupt();
            await(exited);
            check(interrupt ? failure.get() instanceof InterruptedException : failure.get() instanceof TimeoutException,
                    "wrong partial-acquisition outcome: " + failure.get());
            check(locks[first].tryLock(), "partial acquisition leaked first lock");
            locks[first].unlock();
        } finally {
            locks[second].unlock();
            waiter.interrupt();
            waiter.join(5000);
            check(!waiter.isAlive(), "waiter did not stop");
        }
    }

    static void transferAtomicity() throws Exception {
        Ledger ledger = new Ledger(Map.of("a", 1000L, "b", 1000L));
        Map<String, Long> before = Map.copyOf(ledger.balances);
        try {
            ledger.transfer("a", "b", 1001, Duration.ofSeconds(1));
            throw new AssertionError("insufficient balance accepted");
        } catch (IllegalStateException expected) {
            check(ledger.balances.equals(before), "insufficient balance changed state");
        }
        Ledger overflow = new Ledger(Map.of("a", 2L, "b", Long.MAX_VALUE));
        before = Map.copyOf(overflow.balances);
        try {
            overflow.transfer("a", "b", 1, Duration.ofSeconds(1));
            throw new AssertionError("overflow accepted");
        } catch (ArithmeticException expected) {
            check(overflow.balances.equals(before), "overflow left a partial debit");
        }
        ExecutorService pool = Executors.newFixedThreadPool(3);
        CyclicBarrier start = new CyclicBarrier(4);
        try {
            List<Future<?>> runs = new ArrayList<>();
            for (List<String> order : List.of(List.of("a", "b"), List.of("b", "a")))
                runs.add(pool.submit(() -> {
                    start.await(5, TimeUnit.SECONDS);
                    for (int i = 0; i < 500; i++)
                        ledger.transfer(order.get(0), order.get(1), 1, Duration.ofSeconds(5));
                    return null;
                }));
            runs.add(pool.submit(() -> {
                start.await(5, TimeUnit.SECONDS);
                for (int i = 0; i < 1000; i++)
                    ledger.stripes.withKeys(List.of("a", "b"), Duration.ofSeconds(5), () ->
                            check(ledger.balances.get("a") + ledger.balances.get("b") == 2000,
                                    "locked read broke conservation"));
                return null;
            }));
            start.await(5, TimeUnit.SECONDS);
            for (Future<?> run : runs) run.get(5, TimeUnit.SECONDS);
            check(ledger.balances.get("a") == 1000 && ledger.balances.get("b") == 1000,
                    "opposite transfers lost balances");
        } finally {
            pool.shutdownNow();
            check(pool.awaitTermination(5, TimeUnit.SECONDS), "transfer pool did not stop");
        }
    }

    static void immutableSnapshots() throws Exception {
        SnapshotModel model = new SnapshotModel();
        Map<String, Integer> original = new HashMap<>(Map.of("vip", 1));
        SnapshotModel.Rules copied = new SnapshotModel.Rules(original, 0);
        original.put("vip", 2);
        check(copied.rates().get("vip") == 1, "snapshot retained mutable input map");
        try {
            copied.rates().put("vip", 3);
            throw new AssertionError("snapshot is mutable");
        } catch (UnsupportedOperationException expected) { /* immutable map contract */ }
        ExecutorService pool = Executors.newFixedThreadPool(2);
        CyclicBarrier start = new CyclicBarrier(3);
        AtomicInteger remaining = new AtomicInteger(2);
        try {
            List<Future<?>> runs = new ArrayList<>();
            for (String tier : List.of("a", "b"))
                runs.add(pool.submit(() -> {
                    try {
                        start.await(5, TimeUnit.SECONDS);
                        for (int i = 1; i <= 500; i++) model.setRate(tier, i);
                    } finally { remaining.decrementAndGet(); }
                    return null;
                }));
            start.await(5, TimeUnit.SECONDS);
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
            do {
                SnapshotModel.Rules observed = model.current.get();
                check(observed.version() == observed.rates().getOrDefault("a", 0)
                        + observed.rates().getOrDefault("b", 0), "mixed snapshot versions");
                check(System.nanoTime() < deadline, "snapshot writers did not finish");
            } while (remaining.get() > 0);
            for (Future<?> run : runs) run.get(5, TimeUnit.SECONDS);
            check(model.current.get().version() == 1000, "CAS update lost changes");
        } finally {
            pool.shutdownNow();
            check(pool.awaitTermination(5, TimeUnit.SECONDS), "snapshot pool did not stop");
        }
    }

    static final class FakeHttpClient extends HttpClient {
        final CountDownLatch cancelled = new CountDownLatch(1);
        final AtomicInteger submitted = new AtomicInteger();
        final CompletableFuture<HttpResponse<Void>> operation = new CompletableFuture<>() {
            @Override public boolean cancel(boolean interrupt) {
                boolean outcome = super.cancel(interrupt);
                cancelled.countDown();
                return outcome;
            }
        };
        @Override public Optional<CookieHandler> cookieHandler() { return Optional.empty(); }
        @Override public Optional<Duration> connectTimeout() { return Optional.empty(); }
        @Override public Redirect followRedirects() { return Redirect.NEVER; }
        @Override public Optional<ProxySelector> proxy() { return Optional.empty(); }
        @Override public SSLContext sslContext() { throw new UnsupportedOperationException(); }
        @Override public SSLParameters sslParameters() { return new SSLParameters(); }
        @Override public Optional<Authenticator> authenticator() { return Optional.empty(); }
        @Override public Version version() { return Version.HTTP_1_1; }
        @Override public Optional<Executor> executor() { return Optional.empty(); }
        @Override public <T> HttpResponse<T> send(HttpRequest request, HttpResponse.BodyHandler<T> handler)
                throws IOException { throw new UnsupportedOperationException(); }
        @Override @SuppressWarnings("unchecked")
        public <T> CompletableFuture<HttpResponse<T>> sendAsync(HttpRequest request,
                HttpResponse.BodyHandler<T> handler) {
            submitted.incrementAndGet();
            return (CompletableFuture<HttpResponse<T>>) (CompletableFuture<?>) operation;
        }
        @Override public <T> CompletableFuture<HttpResponse<T>> sendAsync(HttpRequest request,
                HttpResponse.BodyHandler<T> handler, HttpResponse.PushPromiseHandler<T> pushes) {
            return sendAsync(request, handler);
        }
    }

    static void cancellationAndTimeout() throws Exception {
        Probe probe = new Probe();
        URI uri = URI.create("https://example.invalid/health");
        FakeHttpClient direct = new FakeHttpClient();
        CompletableFuture<?> result = probe.probe(direct, uri, Duration.ofSeconds(5));
        check(result.cancel(true), "public probe did not cancel");
        await(direct.cancelled);
        check(direct.operation.isCancelled(), "probe cancellation did not reach operation");
        FakeHttpClient timeout = new FakeHttpClient();
        try {
            probe.probe(timeout, uri, Duration.ofMillis(20)).get(5, TimeUnit.SECONDS);
            throw new AssertionError("probe never timed out");
        } catch (ExecutionException expected) {
            check(expected.getCause() instanceof TimeoutException, "wrong timeout cause");
        }
        await(timeout.cancelled);
        check(timeout.operation.isCancelled(), "timed out operation remained active");
        FakeHttpClient invalid = new FakeHttpClient();
        try {
            probe.probe(invalid, uri, Duration.ZERO);
            throw new AssertionError("expired budget accepted");
        } catch (IllegalArgumentException expected) {
            check(invalid.submitted.get() == 0, "invalid budget started IO");
        }
        FakeHttpClient tiny = new FakeHttpClient();
        probe.probe(tiny, uri, Duration.ofNanos(1));
        await(tiny.cancelled);
        check(tiny.submitted.get() == 1, "positive sub-millisecond budget rejected");

        CompletableFuture<String> source = new CompletableFuture<>();
        source.thenApply(String::length).cancel(true);
        check(!source.isDone(), "downstream cancellation unexpectedly changed source");
        CompletableFuture<Integer> dependent = source.thenApply(String::length);
        source.cancel(true);
        try {
            dependent.join();
            throw new AssertionError("upstream cancellation lost");
        } catch (CompletionException expected) {
            check(expected.getCause() instanceof CancellationException, "wrapped cancellation lost");
        }
        CompletableFuture<Void> failed = new CompletableFuture<>();
        CompletableFuture<Void> pending = new CompletableFuture<>();
        CompletableFuture<Void> all = CompletableFuture.allOf(failed, pending);
        failed.completeExceptionally(new IOException("downstream failed"));
        check(!all.isDone(), "allOf unexpectedly failed before all inputs finished");
        all.cancel(true);
        check(!pending.isDone(), "aggregate cancellation unexpectedly reached sibling");
        pending.complete(null);
    }

    public static void main(String[] args) throws Exception {
        lockOrderingAndCleanup();
        partialAcquireExit(false);
        partialAcquireExit(true);
        transferAtomicity();
        immutableSnapshots();
        cancellationAndTimeout();
        System.out.println("PASS: 6 Java mechanism groups; no Spring/database integration executed.");
    }
}
