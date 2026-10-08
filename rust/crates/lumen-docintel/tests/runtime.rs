use lumen_docintel_core::CoreError;
use lumen_docintel_core::runtime::{Budget, Cancellation, Context, Runtime};
use std::sync::{Arc, Barrier};

#[test]
fn budgets_cancellation_and_panic_leave_pool_usable() {
    let runtime = Runtime::new(2, 1).unwrap();
    let units = vec!["healthy".to_owned()];
    let budget = Budget {
        max_memory_bytes: 4,
        ..Budget::default()
    };
    assert!(matches!(
        runtime.copy_units(&units, budget, Cancellation::default()),
        Err(CoreError::Budget)
    ));
    let cancelled = Cancellation::default();
    cancelled.cancel();
    assert!(matches!(
        runtime.copy_units(&units, Budget::default(), cancelled),
        Err(CoreError::Cancelled)
    ));
    let expired = Budget {
        timeout_ms: 0,
        ..Budget::default()
    };
    assert!(matches!(
        runtime.copy_units(&units, expired, Cancellation::default()),
        Err(CoreError::Budget)
    ));
    let context = Context::new(Budget::default(), Cancellation::default()).unwrap();
    let panicked = runtime.execute(&context, &[1], |_, _| -> Result<_, CoreError> {
        panic!("controlled unit panic")
    });
    assert!(matches!(panicked, Err(CoreError::Panic)));
    let result = runtime
        .copy_units(&units, Budget::default(), Cancellation::default())
        .unwrap();
    assert_eq!(result[0].value, "healthy");
}

#[test]
fn cancellation_handshake_and_inflight_admission_do_not_deadlock() {
    let runtime = Arc::new(Runtime::new(2, 1).unwrap());
    let entered = Arc::new(Barrier::new(2));
    let release = Arc::new(Barrier::new(2));
    let token = Cancellation::default();
    let context = Context::new(Budget::default(), token.clone()).unwrap();
    let worker = {
        let runtime = runtime.clone();
        let entered = entered.clone();
        let release = release.clone();
        std::thread::spawn(move || {
            runtime.execute(&context, &[1], |_, ctx| {
                entered.wait();
                release.wait();
                ctx.checkpoint()?;
                ctx.account(1, 0)
            })
        })
    };
    entered.wait();
    assert!(matches!(
        runtime.copy_units(
            &["other".into()],
            Budget::default(),
            Cancellation::default()
        ),
        Err(CoreError::Budget)
    ));
    token.cancel();
    release.wait();
    assert!(matches!(worker.join().unwrap(), Err(CoreError::Cancelled)));
    assert!(
        runtime
            .copy_units(
                &["other".into()],
                Budget::default(),
                Cancellation::default()
            )
            .is_ok()
    );
}

#[test]
fn parallel_documents_preserve_failures_and_order() {
    let runtime = Runtime::new(2, 2).unwrap();
    let docs = vec![vec!["x".repeat(500)], vec!["ok".into()]];
    let budget = Budget {
        max_output_chars: 10,
        ..Budget::default()
    };
    let results = runtime.copy_batch(&docs, budget);
    assert!(matches!(&results[0], Err(CoreError::Budget)));
    assert_eq!(results[1].as_ref().unwrap()[0].value, "ok");
}
