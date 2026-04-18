use std::env;

pub struct Config {
    pub name: String,
}

pub enum Status {
    Done,
    InProgress,
}

pub fn load() -> Config {
    let name = env::var("APP_NAME").unwrap_or_default();
    Config { name }
}

pub trait Runner {
    fn run(&self);
}
