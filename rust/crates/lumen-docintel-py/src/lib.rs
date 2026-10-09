//! The sole Python/native conversion boundary.
use lumen_docintel_core::CoreError;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

pyo3::create_exception!(lumen_docintel, DocIntelError, PyException);
pyo3::create_exception!(lumen_docintel, DocIntelInvalidInputError, DocIntelError);
pyo3::create_exception!(lumen_docintel, DocIntelUnsupportedError, DocIntelError);
pyo3::create_exception!(lumen_docintel, DocIntelParseError, DocIntelError);
pyo3::create_exception!(lumen_docintel, DocIntelBudgetError, DocIntelError);
pyo3::create_exception!(lumen_docintel, DocIntelCancelledError, DocIntelError);
pyo3::create_exception!(lumen_docintel, DocIntelInternalError, DocIntelError);
pyo3::create_exception!(lumen_docintel, DocIntelPanicError, DocIntelError);

fn map_error(error: CoreError) -> PyErr {
    let message = error.to_string();
    match error {
        CoreError::InvalidInput => DocIntelInvalidInputError::new_err(message),
        CoreError::Unsupported => DocIntelUnsupportedError::new_err(message),
        CoreError::Parse => DocIntelParseError::new_err(message),
        CoreError::Budget => DocIntelBudgetError::new_err(message),
        CoreError::Cancelled => DocIntelCancelledError::new_err(message),
        CoreError::Internal => DocIntelInternalError::new_err(message),
        CoreError::Panic => DocIntelPanicError::new_err(message),
    }
}

fn compute<T: Send>(
    py: Python<'_>,
    f: impl FnOnce() -> Result<T, CoreError> + Send,
) -> PyResult<T> {
    let result = py.detach(|| catch_unwind(AssertUnwindSafe(f)));
    match result {
        Ok(Ok(value)) => Ok(value),
        Ok(Err(error)) => {
            let exception = map_error(error.clone());
            exception
                .value(py)
                .setattr("code", format!("docintel_{error:?}").to_ascii_lowercase())?;
            Err(exception)
        }
        Err(_) => {
            let exception = DocIntelPanicError::new_err("native computation panicked");
            exception.value(py).setattr("code", "docintel_panic")?;
            Err(exception)
        }
    }
}

#[pyfunction]
fn core_version() -> &'static str {
    lumen_docintel_core::VERSION
}

#[pyfunction]
fn extract_csv(
    py: Python<'_>,
    data: Py<PyBytes>,
    budget_json: String,
    _mime: String,
) -> PyResult<String> {
    let bytes = data.bind(py).as_bytes();
    compute(py, || {
        let budget = serde_json::from_str::<lumen_docintel_core::runtime::Budget>(&budget_json)
            .map_err(|_| CoreError::InvalidInput)?;
        let document = lumen_docintel_core::formats::csv::parse(bytes, budget.into())?;
        serde_json::to_string(&lumen_docintel_core::canonical::render(document)?)
            .map_err(|_| CoreError::Internal)
    })
}

#[pyfunction]
fn _test_error(py: Python<'_>) -> PyResult<()> {
    compute(py, || Err(CoreError::InvalidInput))
}

#[pyfunction]
fn _test_panic(py: Python<'_>) -> PyResult<()> {
    compute(py, || panic!("controlled bridge panic"))
}

#[derive(Default)]
struct HandshakeState {
    entered: bool,
    released: bool,
    finished: bool,
}

#[pyclass(name = "_Handshake", skip_from_py_object)]
#[derive(Clone, Default)]
struct Handshake {
    shared: Arc<(Mutex<HandshakeState>, Condvar)>,
}

#[pymethods]
impl Handshake {
    #[new]
    fn new() -> Self {
        Self::default()
    }

    fn wait_entered(&self, py: Python<'_>) -> PyResult<bool> {
        let shared = self.shared.clone();
        compute(py, move || {
            let (lock, cv) = &*shared;
            let state = lock.lock().map_err(|_| CoreError::Internal)?;
            let (state, _) = cv
                .wait_timeout_while(state, Duration::from_secs(5), |s| !s.entered)
                .map_err(|_| CoreError::Internal)?;
            Ok(state.entered && !state.finished)
        })
    }

    fn release(&self) -> PyResult<()> {
        let (lock, cv) = &*self.shared;
        let mut state = lock.lock().map_err(|_| map_error(CoreError::Internal))?;
        state.released = true;
        cv.notify_all();
        Ok(())
    }
}

#[pyfunction]
fn _test_wait(py: Python<'_>, gate: &Handshake) -> PyResult<()> {
    let shared = gate.shared.clone();
    compute(py, move || {
        let (lock, cv) = &*shared;
        let mut state = lock.lock().map_err(|_| CoreError::Internal)?;
        state.entered = true;
        cv.notify_all();
        let (mut state, _) = cv
            .wait_timeout_while(state, Duration::from_secs(5), |s| !s.released)
            .map_err(|_| CoreError::Internal)?;
        state.finished = true;
        if state.released {
            Ok(())
        } else {
            Err(CoreError::Budget)
        }
    })
}

#[pyfunction]
fn render_document(py: Python<'_>, document_json: String) -> PyResult<String> {
    compute(py, || {
        lumen_docintel_core::canonical::render_json(&document_json)
    })
}

#[pyfunction]
#[pyo3(signature = (data, declared_mime=None))]
fn detect_format(
    py: Python<'_>,
    data: &Bound<'_, PyBytes>,
    declared_mime: Option<String>,
) -> PyResult<String> {
    let bytes = data.as_bytes();
    compute(py, || {
        lumen_docintel_core::detection::detect_json(bytes, declared_mime.as_deref())
    })
}

#[pyclass(name = "CancellationToken", skip_from_py_object)]
#[derive(Clone, Default)]
struct CancellationToken(lumen_docintel_core::runtime::Cancellation);
#[pymethods]
impl CancellationToken {
    #[new]
    fn new() -> Self {
        Self::default()
    }
    fn cancel(&self) {
        self.0.cancel();
    }
}

#[pyclass(name = "Runtime", frozen)]
struct NativeRuntime(lumen_docintel_core::runtime::Runtime);
#[pymethods]
impl NativeRuntime {
    #[new]
    fn new(py: Python<'_>, threads: usize, max_documents: usize) -> PyResult<Self> {
        compute(py, || {
            lumen_docintel_core::runtime::Runtime::new(threads, max_documents).map(Self)
        })
    }
    fn open_document(
        &self,
        py: Python<'_>,
        budget_json: String,
        token: &CancellationToken,
    ) -> PyResult<DocumentSession> {
        let cancellation = token.0.clone();
        compute(py, || {
            lumen_docintel_core::runtime::context_json(&budget_json, cancellation)
                .map(DocumentSession)
        })
    }
    fn run_window(
        &self,
        py: Python<'_>,
        units_json: String,
        session: &DocumentSession,
    ) -> PyResult<String> {
        let result = compute(py, || self.0.run_context_json(&units_json, &session.0));
        if let Err(error) = &result {
            error.value(py).setattr(
                "diagnostics_json",
                session.0.stats_json().map_err(map_error)?,
            )?;
        }
        result
    }
    fn run_units(
        &self,
        py: Python<'_>,
        units_json: String,
        budget_json: String,
        token: &CancellationToken,
    ) -> PyResult<String> {
        let cancellation = token.0.clone();
        let context = compute(py, || {
            lumen_docintel_core::runtime::context_json(&budget_json, cancellation)
        })?;
        let result = compute(py, || self.0.run_context_json(&units_json, &context));
        if let Err(error) = &result {
            error
                .value(py)
                .setattr("diagnostics_json", context.stats_json().map_err(map_error)?)?;
        }
        result
    }
}

#[pyclass(frozen)]
struct DocumentSession(lumen_docintel_core::runtime::Context);

#[pymodule]
fn lumen_docintel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("DocIntelError", m.py().get_type::<DocIntelError>())?;
    m.add(
        "DocIntelInvalidInputError",
        m.py().get_type::<DocIntelInvalidInputError>(),
    )?;
    m.add(
        "DocIntelUnsupportedError",
        m.py().get_type::<DocIntelUnsupportedError>(),
    )?;
    m.add(
        "DocIntelParseError",
        m.py().get_type::<DocIntelParseError>(),
    )?;
    m.add(
        "DocIntelBudgetError",
        m.py().get_type::<DocIntelBudgetError>(),
    )?;
    m.add(
        "DocIntelCancelledError",
        m.py().get_type::<DocIntelCancelledError>(),
    )?;
    m.add(
        "DocIntelInternalError",
        m.py().get_type::<DocIntelInternalError>(),
    )?;
    m.add(
        "DocIntelPanicError",
        m.py().get_type::<DocIntelPanicError>(),
    )?;
    m.add_function(wrap_pyfunction!(render_document, m)?)?;
    m.add_function(wrap_pyfunction!(detect_format, m)?)?;
    m.add_class::<CancellationToken>()?;
    m.add_class::<NativeRuntime>()?;
    m.add_class::<DocumentSession>()?;
    m.add_class::<Handshake>()?;
    m.add_function(wrap_pyfunction!(core_version, m)?)?;
    m.add_function(wrap_pyfunction!(extract_csv, m)?)?;
    m.add_function(wrap_pyfunction!(_test_error, m)?)?;
    m.add_function(wrap_pyfunction!(_test_panic, m)?)?;
    m.add_function(wrap_pyfunction!(_test_wait, m)?)?;
    Ok(())
}
