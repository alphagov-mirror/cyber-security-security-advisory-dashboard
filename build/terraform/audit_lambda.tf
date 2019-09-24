# Lambda

resource "aws_lambda_function" "github_audit_collector_lambda" {
  filename         = "../github_audit_lambda_package.zip"
  source_code_hash = "${filebase64sha256("../github_audit_lambda_package.zip")}"
  function_name    = "github_audit_collector"
  role             = "${aws_iam_role.github_audit_lambda_exec_role.arn}"
  handler          = "audit_lambda.cronable_vulnerability_audit"
  runtime          = "${var.runtime}"

  environment {
    variables = {
      SECRET_KEY = "${random_string.password.result}"
      FLASK_ENV  = "${var.Environment}"
    }
  }

  vpc_config {
    subnet_ids = ["${aws_default_subnet.z1.id}", "${aws_default_subnet.z2.id}", "${aws_default_subnet.z3.id}"]
    security_group_ids = ["${aws_security_group.github_audit_alb_ingress.id}", "${aws_security_group.github_audit_alb_egress.id}"]
  }

  tags = {
    Service = "${var.Service}"
    Environment = "${var.Environment}"
    SvcOwner = "${var.SvcOwner}"
    DeployedUsing = "${var.DeployedUsing}"
    SvcCodeURL = "${var.SvcCodeURL}"
  }
}


resource "aws_cloudwatch_event_rule" "github_audit_lambda_24_hours" {
    name = "github-audit-24-hours"
    description = "Fire github audit every 24 hours"
    schedule_expression = "cron(0 23 * * * *)"
}

resource "aws_cloudwatch_event_target" "github_audit_lambda_24_hours_tg" {
    rule = "${aws_cloudwatch_event_rule.github_audit_lambda_24_hours.name}"
    arn = "${aws_lambda_function.github_audit_collector_lambda.arn}"
}

resource "aws_lambda_permission" "download_nvd_lambda_allow_cloudwatch" {
    statement_id = "AllowExecutionFromCloudWatch"
    action = "lambda:InvokeFunction"
    function_name = "${aws_lambda_function.github_audit_collector_lambda.function_name}"
    principal = "events.amazonaws.com"
    source_arn = "${aws_cloudwatch_event_rule.github_audit_lambda_24_hours.arn}"
}