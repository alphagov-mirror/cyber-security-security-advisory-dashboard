data "template_file" "interface_lambda_trust" {
  template = "${file("${path.module}/json/interface_lambda/trust.json")}"
  vars {}
}

data "template_file" "interface_lambda_policy" {
  template = "${file("${path.module}/json/interface_lambda/policy.json")}"
  vars {
    region              = "${var.region}"
    account_id          = "${data.aws_caller_identity.current.account_id}"
    storage_bucket_arn  = "${aws_s3_bucket.s3_storage.arn}"
  }
}

resource "aws_iam_role" "github_interface_lambda_exec_role" {
  name = "github_interface_lambda_exec_role"
  assume_role_policy = "${data.template_file.interface_lambda_trust.rendered}"

  tags = {
    Service = "${var.Service}"
    Environment = "${var.Environment}"
    SvcOwner = "${var.SvcOwner}"
    DeployedUsing = "${var.DeployedUsing}"
    SvcCodeURL = "${var.SvcCodeURL}"
  }
}

resource "aws_iam_role_policy" "github_interface_lambda_exec_role_policy" {
  name = "github_interface_lambda_exec_role_policy"
  role = "${aws_iam_role.github_interface_lambda_exec_role.id}"
  policy = "${data.template_file.interface_lambda_policy.rendered}"
}

resource "aws_iam_role_policy_attachment" "github_interface_lambda_exec_role_policy_attach" {
  role       = "${aws_iam_role.github_interface_lambda_exec_role.name}"
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

