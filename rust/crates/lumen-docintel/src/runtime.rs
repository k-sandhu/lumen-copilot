//! Bounded parallel computation substrate; Python retains I/O and orchestration.
use crate::CoreError;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU8, AtomicUsize, Ordering},
};
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Budget {
    pub max_input_bytes: usize,
    pub max_memory_bytes: usize,
    pub max_output_chars: usize,
    pub max_work_units: usize,
    pub timeout_ms: u64,
}
impl Default for Budget {
    fn default() -> Self {
        Self {
            max_input_bytes: 32 * 1024 * 1024,
            max_memory_bytes: 128 * 1024 * 1024,
            max_output_chars: 2_000_000,
            max_work_units: 100_000,
            timeout_ms: 30_000,
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct Cancellation(Arc<AtomicBool>);
impl Cancellation {
    pub fn cancel(&self) {
        self.0.store(true, Ordering::Release);
    }
    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }
}

#[derive(Debug, Default)]
struct Counters {
    memory: AtomicUsize,
    peak: AtomicUsize,
    work: AtomicUsize,
    output: AtomicUsize,
    input: AtomicUsize,
    pages: AtomicUsize,
    glyphs: AtomicUsize,
    reason: AtomicU8,
}
#[derive(Debug)]
pub struct Reservation {
    bytes: usize,
    counters: Arc<Counters>,
}
impl Drop for Reservation {
    fn drop(&mut self) {
        self.counters.memory.fetch_sub(self.bytes, Ordering::AcqRel);
    }
}

#[derive(Debug)]
pub struct Accounted<T> {
    pub value: T,
    _reservation: Reservation,
}
pub type UnitBatch<T> = Accounted<Vec<Accounted<T>>>;
pub type DocumentResult<T> = Result<UnitBatch<T>, CoreError>;

impl<T> std::ops::Deref for Accounted<T> {
    type Target = T;
    fn deref(&self) -> &T {
        &self.value
    }
}
impl<T: Serialize> Serialize for Accounted<T> {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        self.value.serialize(serializer)
    }
}

#[derive(Debug, Clone)]
pub struct Context {
    budget: Budget,
    deadline: Instant,
    token: Cancellation,
    counters: Arc<Counters>,
}
#[derive(Debug, Clone, Copy, Serialize)]
pub struct Stats {
    pub peak_accounted_bytes: usize,
    pub work_units: usize,
    pub output_chars: usize,
    pub source_input_bytes: usize,
    pub pages: usize,
    pub glyphs: usize,
    pub limit: Option<&'static str>,
}

fn charge(counter: &AtomicUsize, amount: usize, limit: usize) -> Result<usize, CoreError> {
    counter
        .try_update(Ordering::AcqRel, Ordering::Acquire, |n| {
            n.checked_add(amount).filter(|v| *v <= limit)
        })
        .map(|prior| prior + amount)
        .map_err(|_| CoreError::Budget)
}
impl Context {
    /// Safe progress counters, including on a failed document; no source identities.
    pub fn pdf_page(&self) {
        self.counters.pages.fetch_add(1, Ordering::AcqRel);
    }
    pub fn pdf_glyph(&self) {
        self.counters.glyphs.fetch_add(1, Ordering::AcqRel);
    }
    pub fn structural_limit(&self) -> CoreError {
        self.failure(9, CoreError::Budget)
    }
    pub fn new(budget: Budget, token: Cancellation) -> Result<Self, CoreError> {
        let deadline = Instant::now()
            .checked_add(Duration::from_millis(budget.timeout_ms))
            .ok_or(CoreError::InvalidInput)?;
        Ok(Self {
            budget,
            deadline,
            token,
            counters: Arc::new(Counters::default()),
        })
    }
    fn failure(&self, code: u8, error: CoreError) -> CoreError {
        let _ = self
            .counters
            .reason
            .compare_exchange(0, code, Ordering::AcqRel, Ordering::Acquire);
        error
    }
    pub fn stats_json(&self) -> Result<String, CoreError> {
        serde_json::to_string(&self.stats()).map_err(|_| CoreError::Internal)
    }
    pub fn checkpoint(&self) -> Result<(), CoreError> {
        if self.token.is_cancelled() {
            Err(self.failure(7, CoreError::Cancelled))
        } else if Instant::now() >= self.deadline {
            Err(self.failure(2, CoreError::Budget))
        } else {
            Ok(())
        }
    }
    pub fn reserve(&self, bytes: usize) -> Result<Reservation, CoreError> {
        self.checkpoint()?;
        let current = charge(&self.counters.memory, bytes, self.budget.max_memory_bytes)
            .map_err(|e| self.failure(1, e))?;
        self.counters.peak.fetch_max(current, Ordering::AcqRel);
        Ok(Reservation {
            bytes,
            counters: self.counters.clone(),
        })
    }
    /// For already allocated/inert values. Use allocate() before a new allocation.
    pub fn account<T>(&self, value: T, bytes: usize) -> Result<Accounted<T>, CoreError> {
        Ok(Accounted {
            value,
            _reservation: self.reserve(bytes)?,
        })
    }
    pub fn allocate<T>(
        &self,
        bytes: usize,
        f: impl FnOnce() -> T,
    ) -> Result<Accounted<T>, CoreError> {
        let reservation = self.reserve(bytes)?;
        Ok(Accounted {
            value: f(),
            _reservation: reservation,
        })
    }
    pub fn work(&self, units: usize) -> Result<(), CoreError> {
        self.checkpoint()?;
        charge(&self.counters.work, units, self.budget.max_work_units)
            .map_err(|e| self.failure(3, e))?;
        Ok(())
    }
    pub fn input(&self, bytes: usize) -> Result<(), CoreError> {
        charge(&self.counters.input, bytes, self.budget.max_input_bytes)
            .map_err(|e| self.failure(4, e))?;
        Ok(())
    }
    pub fn output(&self, chars: usize) -> Result<(), CoreError> {
        charge(&self.counters.output, chars, self.budget.max_output_chars)
            .map_err(|e| self.failure(5, e))?;
        Ok(())
    }
    pub fn stats(&self) -> Stats {
        Stats {
            peak_accounted_bytes: self.counters.peak.load(Ordering::Acquire),
            work_units: self.counters.work.load(Ordering::Acquire),
            output_chars: self.counters.output.load(Ordering::Acquire),
            source_input_bytes: self.counters.input.load(Ordering::Acquire),
            pages: self.counters.pages.load(Ordering::Acquire),
            glyphs: self.counters.glyphs.load(Ordering::Acquire),
            limit: match self.counters.reason.load(Ordering::Acquire) {
                1 => Some("memory"),
                2 => Some("time"),
                3 => Some("work"),
                4 => Some("input"),
                5 => Some("output"),
                6 => Some("admission"),
                7 => Some("cancelled"),
                8 => Some("panic"),
                9 => Some("structure"),
                _ => None,
            },
        }
    }
}

struct Admission<'a>(&'a AtomicUsize);
impl Drop for Admission<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::AcqRel);
    }
}

pub struct Runtime {
    pool: rayon::ThreadPool,
    max_documents: usize,
    active: AtomicUsize,
}
impl Runtime {
    pub fn new(threads: usize, max_documents: usize) -> Result<Self, CoreError> {
        if !(1..=64).contains(&threads) || !(1..=128).contains(&max_documents) {
            return Err(CoreError::InvalidInput);
        }
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(threads)
            .thread_name(|n| format!("lumen-docintel-{n}"))
            .build()
            .map_err(|_| CoreError::Internal)?;
        Ok(Self {
            pool,
            max_documents,
            active: AtomicUsize::new(0),
        })
    }
    fn admit(&self) -> Result<Admission<'_>, CoreError> {
        charge(&self.active, 1, self.max_documents)?;
        Ok(Admission(&self.active))
    }
    fn map<T: Sync, U: Send, F>(&self, ctx: &Context, items: &[T], f: &F) -> DocumentResult<U>
    where
        F: Fn(&T, &Context) -> Result<Accounted<U>, CoreError> + Sync,
    {
        let bytes = items
            .len()
            .checked_mul(std::mem::size_of::<Accounted<U>>())
            .and_then(|n| n.checked_mul(2))
            .ok_or(CoreError::Budget)?;
        let reservation = ctx.reserve(bytes)?;
        let values = self.pool.install(|| {
            items
                .par_iter()
                .map(|item| {
                    catch_unwind(AssertUnwindSafe(|| {
                        ctx.work(1)?;
                        let value = f(item, ctx)?;
                        ctx.checkpoint()?;
                        Ok(value)
                    }))
                    .unwrap_or_else(|_| Err(ctx.failure(8, CoreError::Panic)))
                })
                .collect::<Result<Vec<_>, CoreError>>()
        })?;
        Ok(Accounted {
            value: values,
            _reservation: reservation,
        })
    }
    pub fn execute<T: Sync, U: Send, F>(
        &self,
        ctx: &Context,
        items: &[T],
        f: F,
    ) -> DocumentResult<U>
    where
        F: Fn(&T, &Context) -> Result<Accounted<U>, CoreError> + Sync,
    {
        let _admission = self.admit().map_err(|e| ctx.failure(6, e))?;
        self.map(ctx, items, &f)
    }
    pub fn execute_stream<T: Sync, U: Send, F, C>(
        &self,
        ctx: &Context,
        input: impl Iterator<Item = T>,
        window: usize,
        f: F,
        mut consume: C,
    ) -> Result<(), CoreError>
    where
        F: Fn(&T, &Context) -> Result<Accounted<U>, CoreError> + Sync,
        C: FnMut(Accounted<Vec<Accounted<U>>>) -> Result<(), CoreError>,
    {
        if !(1..=10_000).contains(&window) {
            return Err(CoreError::InvalidInput);
        }
        let _admission = self.admit().map_err(|e| ctx.failure(6, e))?;
        let mut input = input;
        loop {
            ctx.checkpoint()?;
            let reservation = ctx.reserve(
                window
                    .checked_mul(std::mem::size_of::<T>())
                    .ok_or(CoreError::Budget)?,
            )?;
            let items: Vec<T> = input.by_ref().take(window).collect();
            if items.is_empty() {
                return Ok(());
            }
            consume(self.map(ctx, &items, &f)?)?;
            drop(reservation);
        }
    }
    fn copy(&self, ctx: &Context, units: &[String]) -> DocumentResult<String> {
        let input_bytes = units.iter().try_fold(0usize, |n, s| {
            n.checked_add(s.len()).ok_or(CoreError::Budget)
        })?;
        ctx.input(input_bytes)?;
        let _input = ctx.reserve(input_bytes)?;
        self.execute(ctx, units, |text, ctx| {
            let mut chars: usize = 0;
            for _ in text.chars() {
                chars += 1;
                if chars.is_multiple_of(1024) {
                    ctx.work(1)?;
                }
            }
            ctx.output(chars)?;
            ctx.allocate(text.len(), || text.clone())
        })
    }
    pub fn copy_units(
        &self,
        units: &[String],
        budget: Budget,
        token: Cancellation,
    ) -> DocumentResult<String> {
        self.copy(&Context::new(budget, token)?, units)
    }
    pub fn copy_batch(&self, docs: &[Vec<String>], budget: Budget) -> Vec<DocumentResult<String>> {
        docs.chunks(self.max_documents)
            .flat_map(|wave| {
                self.pool.install(|| {
                    wave.par_iter()
                        .map(|units| self.copy_units(units, budget, Cancellation::default()))
                        .collect::<Vec<_>>()
                })
            })
            .collect()
    }
    pub fn run_json(
        &self,
        input: &str,
        budget_json: &str,
        token: Cancellation,
    ) -> Result<String, CoreError> {
        if input.len() > 32 * 1024 * 1024 || budget_json.len() > 4096 {
            return Err(CoreError::Budget);
        }
        let budget: Budget =
            serde_json::from_str(budget_json).map_err(|_| CoreError::InvalidInput)?;
        self.run_context_json(input, &Context::new(budget, token)?)
    }
    pub fn run_context_json(&self, input: &str, ctx: &Context) -> Result<String, CoreError> {
        if input.len() > 32 * 1024 * 1024 {
            return Err(CoreError::Budget);
        }

        let _raw = ctx.reserve(input.len().checked_mul(2).ok_or(CoreError::Budget)?)?;
        // Bound array capacity before serde allocates it (including empty strings).
        let capacity = (input.len() / 2 + 1).min(100_000).next_power_of_two();
        let _metadata = ctx.reserve(capacity * std::mem::size_of::<String>())?;
        struct UnitsVisitor<'a>(&'a std::cell::Cell<bool>);
        impl<'de> serde::de::Visitor<'de> for UnitsVisitor<'_> {
            type Value = Vec<String>;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("bounded string units")
            }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(
                self,
                mut seq: A,
            ) -> Result<Self::Value, A::Error> {
                let mut values = vec![];
                while let Some(value) = seq.next_element::<String>()? {
                    if values.len() == 100_000 {
                        self.0.set(true);
                        return Err(serde::de::Error::custom("unit budget"));
                    }
                    values.push(value);
                }
                Ok(values)
            }
        }
        let exceeded = std::cell::Cell::new(false);
        let mut deserializer = serde_json::Deserializer::from_str(input);
        let units =
            serde::Deserializer::deserialize_seq(&mut deserializer, UnitsVisitor(&exceeded))
                .map_err(|_| {
                    if exceeded.get() {
                        CoreError::Budget
                    } else {
                        CoreError::InvalidInput
                    }
                })?;
        deserializer.end().map_err(|_| CoreError::InvalidInput)?;
        let output = self.copy(ctx, &units)?;
        let worst_json = output.iter().try_fold(1024usize, |n, s| {
            n.checked_add(s.len().saturating_mul(6) + 64)
                .ok_or(CoreError::Budget)
        })?;
        let _serialized = ctx.reserve(worst_json)?;
        #[derive(Serialize)]
        struct Report<'a> {
            units: &'a Accounted<Vec<Accounted<String>>>,
            diagnostics: Stats,
        }
        let json = serde_json::to_string(&Report {
            units: &output,
            diagnostics: ctx.stats(),
        })
        .map_err(|_| CoreError::Internal)?;
        ctx.checkpoint()?;
        Ok(json)
    }
}

pub fn context_json(budget_json: &str, token: Cancellation) -> Result<Context, CoreError> {
    if budget_json.len() > 4096 {
        return Err(CoreError::Budget);
    }
    let budget = serde_json::from_str(budget_json).map_err(|_| CoreError::InvalidInput)?;
    Context::new(budget, token)
}
